"""Persistent Discord UI views and modals."""

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite
import discord

from cogs.common import (
    BotError,
    active_tournament,
    discord_ts,
    is_organizer_user,
    report_error,
    respond,
)
from nullrush import RelayError
from services import (
    accept_proposal,
    cancel_event,
    deadline_hours,
    extend_deadline,
    force_time,
    guild_timezone,
    open_room,
    propose_time,
    push_result_to_challonge,
    sync_event,
)
from timeparse import TimeParseError, parse_when

log = logging.getLogger(__name__)


# --------------------------------------------------------------- shared bits


async def _opponent_of(bot, tournament_id: int, match: aiosqlite.Row, user_id: int):
    """Return (your participant id, opponent's Discord id) or (None, None)."""
    me = await bot.store.participant_for_discord(tournament_id, user_id)
    if me is None:
        return None, None
    mine = int(me["participant_id"])
    p1, p2 = match["player1_id"], match["player2_id"]
    other = p2 if mine == p1 else p1
    if other is None:
        return mine, None
    mapping = await bot.store.discord_ids_for(tournament_id, [int(other)])
    return mine, mapping.get(int(other))


async def _thread_context(bot, interaction: discord.Interaction):
    """Resolve the match and tournament this thread belongs to."""
    match = await bot.store.match_for_thread(interaction.channel_id)
    if match is None:
        raise BotError("This thread is not linked to a match any more.")
    tournament = await bot.store.get_tournament(int(match["tournament_id"]))
    if tournament is None:
        raise BotError("That tournament no longer exists.")
    return tournament, match


async def refresh_panel(bot, guild: discord.Guild, tournament: aiosqlite.Row) -> None:
    """Repaint the panel message after something on it changed."""
    from ui.embeds import panel_embed

    channel_id, message_id = tournament["channel_id"], tournament["signup_message_id"]
    if not (channel_id and message_id and guild):
        return
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=await panel_embed(bot, tournament))
    except discord.HTTPException:
        pass  # The panel was deleted; posting a new one recreates it.


async def send_proposal(
    bot,
    channel: discord.abc.Messageable,
    tournament: aiosqlite.Row,
    match: aiosqlite.Row,
    *,
    proposer_id: int,
    responder_id: int | None,
    when: datetime,
) -> None:
    """Record a proposed time and post it for the opponent to answer."""
    from ui.embeds import proposal_embed

    tournament_id = int(tournament["challonge_id"])
    proposal_id = await propose_time(
        bot,
        tournament,
        match,
        proposer_id=proposer_id,
        responder_id=responder_id,
        when=when,
    )
    names = await bot.store.participant_names(tournament_id)
    message = await channel.send(
        content=f"<@{responder_id}>" if responder_id else None,
        embed=proposal_embed(
            match,
            names,
            proposer_id=proposer_id,
            responder_id=responder_id,
            when=when,
        ),
        view=ProposalView(bot),
    )
    await bot.store.set_proposal_message(proposal_id, message.id)


class TimeModal(discord.ui.Modal):
    """Asks for a time, parses it in the server's timezone, hands it back."""

    when = discord.ui.TextInput(
        label="When?",
        placeholder="tomorrow 20:00  |  friday 8pm  |  in 90m  |  2026-09-05 19:30",
        max_length=64,
    )

    def __init__(self, bot, *, title: str, on_time) -> None:
        super().__init__(title=title)
        self.bot = bot
        self._on_time = on_time

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tz_name = await guild_timezone(self.bot, interaction.guild_id)
        try:
            moment = parse_when(str(self.when.value), tz_name)
        except TimeParseError as exc:
            await respond(interaction, f"Invalid time: {exc}")
            return
        await self._on_time(interaction, moment)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_error(interaction, error)


class _BaseView(discord.ui.View):
    def __init__(self, bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _disable(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def _require_organiser(self, interaction: discord.Interaction) -> None:
        if not await is_organizer_user(
            self.bot.store, interaction.guild_id, interaction.user
        ):
            raise BotError("Organisers only.")


# ------------------------------------------------------------------ sign-up


class SignupModal(discord.ui.Modal, title="Sign up"):
    """Modal for tournament bracket registration."""

    name = discord.ui.TextInput(
        label="Your name on the bracket",
        placeholder="Exactly as the organiser has it on Challonge",
        required=False,
        max_length=60,
    )

    def __init__(
        self, bot, tournament: aiosqlite.Row, current: str, *, is_signed_up: bool = False
    ) -> None:
        super().__init__()
        self.bot = bot
        self.tournament = tournament
        self.name.default = (current or "")[:60]
        if is_signed_up:
            self.name.label = "Your bracket name (clear to withdraw)"
        else:
            self.name.label = "Your name on the bracket"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            tournament_id = int(self.tournament["challonge_id"])
            typed = str(self.name.value).strip()

            if not typed:
                await self._withdraw(interaction, tournament_id)
                return

            if await self.bot.store.signup_name_taken(
                tournament_id, typed, interaction.user.id
            ):
                await respond(
                    interaction,
                    f"Name already taken: Somebody else already signed up as **{typed}**. If that "
                    "is genuinely your name on the bracket, ask an organiser.",
                )
                return

            # Moving to a different name releases the old bracket entry.
            previous = await self.bot.store.participant_for_discord(
                tournament_id, interaction.user.id
            )
            if previous is not None:
                await self.bot.store.unlink_participant(
                    tournament_id, int(previous["participant_id"])
                )

            await self.bot.store.upsert_signup(
                tournament_id, interaction.user.id, typed
            )
            linked = await self.bot.store.link_signups_to_bracket(tournament_id)

            if linked or await self.bot.store.participant_for_discord(
                tournament_id, interaction.user.id
            ):
                message = f"Signed up as **{typed}** (linked to bracket)."
            else:
                message = (
                    f"Signed up as **{typed}**.\n"
                    "Not linked yet - make sure your name matches on Challonge."
                )
            await respond(interaction, message)

            await refresh_panel(self.bot, interaction.guild, self.tournament)
            # A newly linked player can complete a pairing that was waiting.
            self.bot.dispatch("player_claimed", self.tournament)
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    async def _withdraw(
        self, interaction: discord.Interaction, tournament_id: int
    ) -> None:
        existing = await self.bot.store.participant_for_discord(
            tournament_id, interaction.user.id
        )
        if existing is not None:
            await self.bot.store.unlink_participant(
                tournament_id, int(existing["participant_id"])
            )
        removed = await self.bot.store.remove_signup(
            tournament_id, interaction.user.id
        )
        await respond(
            interaction,
            "Withdrawn. You will not be added to any more match threads.\n"
            "*This does not remove you from the bracket itself; an organiser "
            "does that on Challonge.*"
            if removed
            else "You were not signed up.",
        )
        await refresh_panel(self.bot, interaction.guild, self.tournament)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_error(interaction, error)


# ---------------------------------------------------------------- the panel


class PanelView(_BaseView):
    """The panel posted by /tournament post. The whole bot, in six buttons."""

    @discord.ui.button(
        label="Sign up",
        style=discord.ButtonStyle.success,
        custom_id="tb:panel:signup",
        row=0,
    )
    async def signup(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament = await active_tournament(interaction)
            existing = await self.bot.store.signup_for_discord(
                int(tournament["challonge_id"]), interaction.user.id
            )
            await interaction.response.send_modal(
                SignupModal(
                    self.bot,
                    tournament,
                    existing["name"] if existing else interaction.user.display_name,
                    is_signed_up=existing is not None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Bracket",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:panel:bracket",
        row=0,
    )
    async def bracket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            from ui.embeds import bracket_embed

            tournament = await active_tournament(interaction)
            tournament_id = int(tournament["challonge_id"])
            await interaction.followup.send(
                embed=bracket_embed(
                    tournament,
                    await self.bot.store.list_matches(tournament_id),
                    await self.bot.store.participant_display(tournament_id),
                ),
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="My match",
        style=discord.ButtonStyle.primary,
        custom_id="tb:panel:mine",
        row=0,
    )
    async def my_match(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            from ui.embeds import next_match_embed

            tournament = await active_tournament(interaction)
            tournament_id = int(tournament["challonge_id"])
            mine = await self.bot.store.participant_for_discord(
                tournament_id, interaction.user.id
            )
            signed_up = await self.bot.store.signup_for_discord(
                tournament_id, interaction.user.id
            )
            await interaction.followup.send(
                embed=next_match_embed(
                    tournament,
                    await self.bot.store.next_match_for(
                        tournament_id, interaction.user.id
                    ),
                    await self.bot.store.participant_display(tournament_id),
                    on_bracket=mine is not None,
                    signed_up=signed_up is not None,
                    signup_name=signed_up["name"] if signed_up else None,
                ),
                ephemeral=True,
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)


# -------------------------------------------------------------- match thread


class MatchThreadView(_BaseView):
    """Pinned in every match thread. The whole player interface."""

    @discord.ui.button(
        label="Propose time",
        style=discord.ButtonStyle.primary,
        custom_id="tb:match:propose",
    )
    async def propose(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament, match = await _thread_context(self.bot, interaction)
            tournament_id = int(tournament["challonge_id"])
            mine, opponent_id = await _opponent_of(
                self.bot, tournament_id, match, interaction.user.id
            )
            if mine is None and not await is_organizer_user(
                self.bot.store, interaction.guild_id, interaction.user
            ):
                raise BotError(
                    "Only the two players in this match can propose a time."
                )

            async def on_time(modal_interaction: discord.Interaction, moment):
                await modal_interaction.response.defer()
                await send_proposal(
                    self.bot,
                    modal_interaction.channel,
                    tournament,
                    match,
                    proposer_id=modal_interaction.user.id,
                    responder_id=opponent_id,
                    when=moment,
                )

            await interaction.response.send_modal(
                TimeModal(self.bot, title="Propose a match time", on_time=on_time)
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Clear time",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:match:clear",
    )
    async def clear(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament, match = await _thread_context(self.bot, interaction)
            tournament_id = int(tournament["challonge_id"])
            match_id = int(match["match_id"])

            mine, _ = await _opponent_of(
                self.bot, tournament_id, match, interaction.user.id
            )
            if mine is None and not await is_organizer_user(
                self.bot.store, interaction.guild_id, interaction.user
            ):
                raise BotError("Only the players in this match can do that.")
            if not match["scheduled_at"]:
                raise BotError("There is no agreed time to clear.")

            await interaction.response.defer()
            await self.bot.store.set_match_schedule(tournament_id, match_id, None)
            await self.bot.store.clear_reminders(tournament_id, match_id)
            await self.bot.store.supersede_proposals(tournament_id, match_id)
            await self.bot.store.set_scheduling_status(
                tournament_id, match_id, "pending"
            )
            if match["event_id"]:
                await cancel_event(self.bot, tournament, match)

            await interaction.followup.send(
                f"<@{interaction.user.id}> cleared the agreed time. Propose a "
                "new one; the deadline still applies."
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Feature",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:match:live",
    )
    async def feature(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Organisers only: publish this match as a server event."""
        try:
            await self._require_organiser(interaction)
            tournament, match = await _thread_context(self.bot, interaction)
            tournament_id = int(tournament["challonge_id"])
            match_id = int(match["match_id"])
            now_live = not bool(match["live"])

            await interaction.response.defer()
            await self.bot.store.set_match_live(tournament_id, match_id, now_live)
            refreshed = await self.bot.store.get_match(tournament_id, match_id)
            await sync_event(self.bot, tournament, refreshed)

            if now_live:
                detail = (
                    "A server event is up for the agreed time."
                    if refreshed and refreshed["scheduled_at"]
                    else "The event goes up as soon as you agree a time."
                )
                await interaction.followup.send(f"**Featured match.** {detail}")
            else:
                await interaction.followup.send(
                    "Back to a normal private match. Any server event for it "
                    "has been removed."
                )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)


    @discord.ui.button(
        label="Play in Null Rush",
        style=discord.ButtonStyle.success,
        custom_id="tb:match:play",
    )
    async def play(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Reserve a relay room and post the code. Costs no Challonge quota."""
        try:
            if not self.bot.relay.configured:
                raise BotError(
                    "In-game matches are not set up on this server. An admin "
                    "sets `RELAY_URL` and `RELAY_TOKEN` to turn them on; until "
                    "then, play however you normally would."
                )

            tournament, match = await _thread_context(self.bot, interaction)
            await interaction.response.defer()
            code = await open_room(self.bot, tournament, match)

            from ui.embeds import room_embed

            names = await self.bot.store.participant_display(
                int(tournament["challonge_id"])
            )
            await interaction.followup.send(embed=room_embed(match, names, code))
        except RelayError as exc:
            await report_error(interaction, BotError(str(exc)))
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)


class ResultView(_BaseView):
    """View displayed when a game result is reported."""

    @discord.ui.button(
        label="Enter on Challonge",
        style=discord.ButtonStyle.primary,
        custom_id="tb:result:publish",
    )
    async def publish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            await self._require_organiser(interaction)
            match = await self.bot.store.match_for_thread(interaction.channel_id)
            if match is None:
                raise BotError("This thread is not linked to a match any more.")
            if match["state"] == "complete":
                raise BotError("Challonge already has a result for this match.")
            if not match["scores"]:
                raise BotError(
                    "I have no reported score for this match yet. Enter it on "
                    "challonge.com instead."
                )
            tournament = await self.bot.store.get_tournament(
                int(match["tournament_id"])
            )
            if tournament is None:
                raise BotError("That tournament no longer exists.")

            await interaction.response.defer()
            await push_result_to_challonge(
                self.bot,
                tournament,
                match,
                winner_id=int(match["winner_id"]),
                scores=str(match["scores"]),
            )
            await self._disable(interaction)

            budget = await self.bot.budget.status()
            await interaction.followup.send(
                f"Entered on Challonge by <@{interaction.user.id}>.\n"
                f"*Used 2 requests, {budget.used} of {budget.limit} this month.*"
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Ignore",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:result:ignore",
    )
    async def ignore(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            await self._require_organiser(interaction)
            await interaction.response.defer()
            await self._disable(interaction)
            await interaction.followup.send(
                f"Left alone by <@{interaction.user.id}>. Enter it on "
                "challonge.com if it should count."
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)


# ---------------------------------------------------------------- proposals


class ProposalView(_BaseView):
    """Accept, counter or decline. Answerable only by the opponent."""

    async def _resolve(self, interaction: discord.Interaction):
        proposal = await self.bot.store.proposal_for_message(interaction.message.id)
        if proposal is None:
            raise BotError("I cannot find this proposal any more.")
        if proposal["status"] != "pending":
            raise BotError(
                f"This proposal was already **{proposal['status']}**. Propose a "
                "new time if you still need one."
            )
        tournament = await self.bot.store.get_tournament(
            int(proposal["tournament_id"])
        )
        match = await self.bot.store.get_match(
            int(proposal["tournament_id"]), int(proposal["match_id"])
        )
        if tournament is None or match is None:
            raise BotError("That match no longer exists.")
        return proposal, tournament, match

    async def _check_responder(
        self, interaction: discord.Interaction, proposal: aiosqlite.Row
    ) -> None:
        if interaction.user.id == int(proposal["proposer_id"]):
            raise BotError(
                "You proposed this one, so it is your opponent's turn to answer."
            )
        responder_id = proposal["responder_id"]
        if responder_id is not None and interaction.user.id != int(responder_id):
            if not await is_organizer_user(
                self.bot.store, interaction.guild_id, interaction.user
            ):
                raise BotError("Only the other player in this match can answer.")

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="tb:prop:accept",
    )
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            proposal, tournament, match = await self._resolve(interaction)
            await self._check_responder(interaction, proposal)

            await interaction.response.defer()
            when = await accept_proposal(self.bot, tournament, match, proposal)
            await self._disable(interaction)

            from ui.embeds import agreed_embed

            tournament_id = int(tournament["challonge_id"])
            refreshed = await self.bot.store.get_match(
                tournament_id, int(match["match_id"])
            )
            await interaction.followup.send(
                embed=agreed_embed(
                    refreshed or match,
                    await self.bot.store.participant_names(tournament_id),
                    when,
                )
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Counter",
        style=discord.ButtonStyle.primary,
        custom_id="tb:prop:counter",
    )
    async def counter(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            proposal, tournament, match = await self._resolve(interaction)
            await self._check_responder(interaction, proposal)

            async def on_time(modal_interaction: discord.Interaction, moment):
                await modal_interaction.response.defer()
                await self._disable(interaction)
                await send_proposal(
                    self.bot,
                    modal_interaction.channel,
                    tournament,
                    match,
                    proposer_id=modal_interaction.user.id,
                    responder_id=int(proposal["proposer_id"]),
                    when=moment,
                )

            await interaction.response.send_modal(
                TimeModal(
                    self.bot, title="Suggest a different time", on_time=on_time
                )
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:prop:decline",
    )
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            proposal, tournament, match = await self._resolve(interaction)
            await self._check_responder(interaction, proposal)

            await self.bot.store.set_proposal_status(int(proposal["id"]), "declined")
            await self.bot.store.set_scheduling_status(
                int(tournament["challonge_id"]), int(match["match_id"]), "pending"
            )
            await interaction.response.defer()
            await self._disable(interaction)
            await interaction.followup.send(
                f"<@{interaction.user.id}> cannot make that time. Propose "
                "another one; the deadline is still running.",
                view=MatchThreadView(self.bot),
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)


# --------------------------------------------------------------- escalation


class EscalationView(_BaseView):
    """Organiser controls on a match that blew its scheduling deadline."""

    async def _resolve(self, interaction: discord.Interaction):
        await self._require_organiser(interaction)
        match = await self.bot.store.match_for_escalation_message(
            interaction.message.id
        )
        if match is None:
            raise BotError("I cannot find the match this refers to.")
        tournament = await self.bot.store.get_tournament(int(match["tournament_id"]))
        if tournament is None:
            raise BotError("That tournament no longer exists.")
        return tournament, match

    async def _tell_thread(self, match: aiosqlite.Row, text: str) -> None:
        thread_id = match["thread_id"]
        thread = self.bot.get_channel(int(thread_id)) if thread_id else None
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(text)
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Set a time",
        style=discord.ButtonStyle.danger,
        custom_id="tb:esc:force",
    )
    async def force(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament, match = await self._resolve(interaction)

            async def on_time(modal_interaction: discord.Interaction, moment):
                await modal_interaction.response.defer()
                await force_time(self.bot, tournament, match, moment)
                await self._disable(interaction)
                await modal_interaction.followup.send(
                    f"Time set to {discord_ts(moment)} by "
                    f"<@{modal_interaction.user.id}>."
                )
                await self._tell_thread(
                    match,
                    f"An organiser has set this match for "
                    f"{discord_ts(moment)}. Reminders are on.",
                )

            await interaction.response.send_modal(
                TimeModal(self.bot, title="Set the match time", on_time=on_time)
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Give them longer",
        style=discord.ButtonStyle.primary,
        custom_id="tb:esc:extend",
    )
    async def extend(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament, match = await self._resolve(interaction)
            hours = await deadline_hours(self.bot, interaction.guild_id) or 24

            await interaction.response.defer()
            deadline = await extend_deadline(self.bot, tournament, match, hours)
            await self._disable(interaction)
            await interaction.followup.send(
                f"Deadline extended to {discord_ts(deadline)}."
            )
            await self._tell_thread(
                match,
                f"An organiser gave you until {discord_ts(deadline)} to "
                "agree a time.",
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)

    @discord.ui.button(
        label="Dismiss",
        style=discord.ButtonStyle.secondary,
        custom_id="tb:esc:done",
    )
    async def handled(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        try:
            tournament, match = await self._resolve(interaction)
            await self.bot.store.set_scheduling_status(
                int(tournament["challonge_id"]), int(match["match_id"]), "handled"
            )
            await interaction.response.defer()
            await self._disable(interaction)
            await interaction.followup.send(
                f"Marked as handled by <@{interaction.user.id}>."
            )
        except Exception as exc:  # noqa: BLE001
            await report_error(interaction, exc)
