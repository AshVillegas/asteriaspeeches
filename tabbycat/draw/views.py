import datetime
import logging

from django.views.generic.base import TemplateView, View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest

from actionlog.mixins import LogActionMixin
from actionlog.models import ActionLogEntry
from adjallocation.models import DebateAdjudicator
from divisions.models import Division
from participants.models import Adjudicator, Team
from standings.teams import TeamStandingsGenerator
from tournaments.mixins import PublicTournamentPageMixin, RoundMixin, TournamentMixin
from tournaments.models import Round
from utils.mixins import CacheMixin, ExpectPost, PostOnlyRedirectView, SuperuserRequiredMixin, VueTableMixin
from utils.misc import reverse_round
from utils.tables import TabbycatTableBuilder
from venues.allocator import allocate_venues

from .manager import DrawManager
from .models import Debate, DebateTeam, TeamPositionAllocation
from .dbutils import delete_round_draw
from .utils import get_draw_with_standings

logger = logging.getLogger(__name__)


TPA_MAP = {
    TeamPositionAllocation.POSITION_AFFIRMATIVE: "Aff",
    TeamPositionAllocation.POSITION_NEGATIVE: "Neg",
    None: "—"
}


# ==============================================================================
# Viewing Draw
# ==============================================================================

class DrawTablePage(RoundMixin, VueTableMixin):

    template_name = 'draw_display_by.html'

    def get_page_title(self):
        round = self.get_round()
        return 'Draw for %s' % round.name

    def get_page_emoji(self):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            return '👏'
        else:
            return '😴'

    def get_page_sub_title(self):
        round = self.get_round()
        if round.starts_at:
            return 'debates start at %s' % round.starts_at
        else:
            return ''

    def get_context_data(self, **kwargs):
        kwargs['round'] = self.get_round()
        return super().get_context_data(**kwargs)

    def create_rows(self, draw, table, tournament, by_team=False):
        if by_team:
            draw = list(draw) + list(draw) # Double up the draw
            draw_slice = int(len(draw) / 2) # Top half gets affs; bottom negs
            table.add_team_columns(
                [d.aff_team for d in draw[:draw_slice]] +
                [d.neg_team for d in draw[draw_slice:]],
                hide_institution=True,
                key="Team")

        table.add_debate_venue_columns(draw)
        table.add_team_columns([d.aff_team for d in draw], hide_institution=True, key="Aff")
        table.add_team_columns([d.neg_team for d in draw], hide_institution=True, key="Neg")
        if tournament.pref('enable_division_motions'):
            for debate in draw:
                table.add_motion_column([m.reference for m in debate.division_motions])
        if not tournament.pref('enable_divisions'):
            table.add_debate_adjudicators_column(draw)

    def get_table(self):
        tournament = self.get_tournament()
        round = self.get_round()
        draw = round.get_draw()
        sorting = self.sorting
        print(sorting)
        table = TabbycatTableBuilder(view=self, sort_key=sorting)
        if self.sorting is 'Team':  # Add extra rows
            self.create_rows(draw, table, tournament, by_team=True)
        else:
            self.create_rows(draw, table, tournament)

        return table


class PublicDrawForRound(DrawTablePage, PublicTournamentPageMixin, CacheMixin):

    public_page_preference = 'public_draw'
    sorting = 'Venue'

    def get_template_names(self):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            messages.info(self.request, 'The draw for ' + round.name +
                ' has yet to be released 😴')
            return ["base.html"]
        else:
            return super().get_template_names()

    def get_context_data(self, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_RELEASED:
            kwargs["round"] = self.get_round()
            return super(DrawTablePage, self).get_context_data(**kwargs) # skip DrawTablePage
        else:
            return super().get_context_data(**kwargs)


class PublicDrawForCurrentRound(PublicDrawForRound):

    def get_round(self):
        return self.get_tournament().current_round


class PublicAllDrawsAllTournamentsView(PublicTournamentPageMixin, TemplateView):
    template_name = "public_draw_display_all.html"

    def get_context_data(self, **kwargs):
        t = self.get_tournament()
        all_rounds = list(Round.objects.filter(
            tournament=t, draw_status=Round.STATUS_RELEASED))
        for r in all_rounds:
            r.draw = r.get_draw()
        kwargs['all_rounds'] = all_rounds
        return super().get_context_data(**kwargs)


class AdminDrawDisplayForRoundByVenue(DrawTablePage, LoginRequiredMixin):
    sorting = 'Venue'


class AdminDrawDisplayForRoundByTeam(DrawTablePage, LoginRequiredMixin):
    sorting = 'Team'


# ==============================================================================
# Draw Creating
# ==============================================================================

class AdminDrawEditView(RoundMixin, SuperuserRequiredMixin, VueTableMixin):

    def get_table(self):
        round = self.get_round()
        table = TabbycatTableBuilder(view=self)
        if round.draw_status == round.STATUS_NONE:
            return table
        else:
            draw = round.get_draw()
            table.add_debate_bracket_columns(draw)
            table.add_team_columns([d.aff_team for d in draw], key="Affirmative", hide_institution=True)
            table.add_team_columns([d.neg_team for d in draw], key="Negative", hide_institution=True)
            if round.draw_status == round.STATUS_DRAFT:
                return table
            elif round.draw_status == round.STATUS_CONFIRMED:
                return table
            elif round.draw_status == round.STATUS_RELEASED:
                return table
            else:
                raise

    def get_template_names(self):
        round = self.get_round()
        self.page_emoji = '👀'
        if round.draw_status == round.STATUS_NONE:
            self.page_title = 'No draw for %s' % round.name
            messages.warning(self.request, 'No draw exists yet — go to the ' +
                'check-ins section for this round to generate a draw.')
            return ["base.html"]
        elif round.draw_status == round.STATUS_DRAFT:
            self.page_title = 'Draft draw for %s' % round.name
            return ["draw_draft.html"]
        elif round.draw_status == round.STATUS_CONFIRMED:
            self.page_title = 'Draw for %s' % round.name
            return ["draw_confirmed.html"]
        elif round.draw_status == round.STATUS_RELEASED:
            self.page_title = 'Released draw for %s' % round.name
            return ["draw_confirmed.html"]
        else:
            raise

    def get_context_data(self, **kwargs):
        round = self.get_round()
        kwargs['active_teams'] = round.active_teams.all()
        kwargs['rooms'] = float(kwargs['active_teams'].count()) // 2
        kwargs['active_adjs'] = round.active_adjudicators.count()

        if round.draw_status == round.STATUS_NONE:
            kwargs['all_teams_count'] = Team.objects.filter(tournament=round.tournament).count()
            kwargs['active_venues_count'] = round.active_venues.count()
            if round.prev:
                kwargs['previous_unconfirmed'] = round.prev.get_draw().filter(
                    result_status__in=[Debate.STATUS_NONE, Debate.STATUS_DRAFT]).count()
            self.template = 'draw_none'
        elif round.draw_status == round.STATUS_DRAFT:
            pass
        elif round.draw_status == round.STATUS_CONFIRMED:
            pass
        elif round.draw_status == round.STATUS_RELEASED:
            pass
        return super().get_context_data(**kwargs)


class AdminDrawWithDetailsView(View):

    page_tile = "Draw with Details"
    template = "draw_base.html"

    def get_table(self):
        r = self.get_round()
        draw = r.get_draw()

        teams = r.tournament.team_set.select_related('institution')
        metrics = r.tournament.pref('team_standings_precedence')
        generator = TeamStandingsGenerator(metrics, ('rank', 'subrank'))
        standings = generator.generate(teams, round=r.prev)

        print(standings, draw)

        # for debate in draw:
        #     aff_standing = standings.get_standing(debate.aff_team)
        #     neg_standing = standings.get_standing(debate.neg_team)
        #     debate.metrics = [(a, n) for a, n in zip(aff_standing.itermetrics(), neg_standing.itermetrics())]
        #     if round.is_break_round:
        #         debate.aff_breakrank = BreakingTeam.objects.get(
        #                 break_category=round.break_category,
        #                 team=debate.aff_team.id).break_rank
        #         debate.neg_breakrank = BreakingTeam.objects.get(
        #                 break_category=round.break_category,
        #                 team=debate.neg_team.id).break_rank
        #     else:
        #         if "points" in standings.metric_keys:
        #             debate.aff_is_pullup = abs(aff_standing.metrics["points"] - debate.bracket) >= 1
        #             debate.neg_is_pullup = abs(neg_standing.metrics["points"] - debate.bracket) >= 1
        #         debate.aff_subrank = aff_standing.rankings["subrank"]
        #         debate.neg_subrank = neg_standing.rankings["subrank"


# ==============================================================================
# Draw Status POSTS
# ==============================================================================

class DrawStatusEdit(LogActionMixin, SuperuserRequiredMixin, RoundMixin, PostOnlyRedirectView):

    def get_redirect_url(self):
        return reverse_round('draw', self.get_round())


class CreateDrawView(DrawStatusEdit):

    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CREATE

    def post(self, request, *args, **kwargs):
        round = self.get_round()

        if round.draw_status != round.STATUS_NONE:
            messages.error(request, "Could not create draw for {}, there was already a draw!".format(round.name))
            return super().post(request, *args, **kwargs)

        manager = DrawManager(round)
        manager.create()

        allocate_venues(round)

        self.log_action()
        return super().post(request, *args, **kwargs)


class ConfirmDrawCreationView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_CONFIRM

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_DRAFT:
            return HttpResponseBadRequest("Draw status is not DRAFT")

        round.draw_status = round.STATUS_CONFIRMED
        round.save()
        return super().post(request, *args, **kwargs)


class DrawRegenerateView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_REGENERATE

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        delete_round_draw(round)
        return super().post(request, *args, **kwargs)


class ConfirmDrawRegenerationView(SuperuserRequiredMixin, TemplateView):
    template_name = "draw_confirm_regeneration.html"


class DrawReleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_RELEASE

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status != round.STATUS_CONFIRMED:
            return HttpResponseBadRequest("Draw status is not CONFIRMED")

        round.draw_status = round.STATUS_RELEASED
        round.save()
        return super().post(request, *args, **kwargs)


class DrawUnreleaseView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_DRAW_UNRELEASE

    def post(self, request, *args, **kwargs):
        if round.draw_status != round.STATUS_RELEASED:
            return HttpResponseBadRequest("Draw status is not RELEASED")

        round.draw_status = round.STATUS_CONFIRMED
        round.save()
        return super().post(request, *args, **kwargs)


class SetRoundStartTimeView(DrawStatusEdit):
    action_log_type = ActionLogEntry.ACTION_TYPE_ROUND_START_TIME_SET

    def post(self, request, *args, **kwargs):
        time_text = request.POST["start_time"]
        try:
            time = datetime.datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            messages.error(request, "Sorry, \"{}\" isn't a valid time. It must "
                           "be in 24-hour format, with a colon, for "
                           "example: \"13:57\".".format(time_text))
            return super().post(request, *args, **kwargs)

        round = self.get_round()
        round.starts_at = time
        round.save()
        # Need to call explicitly, since this isn't a form view
        self.log_action()

        return super().post(request, *args, **kwargs)


# ==============================================================================
# Adjudicator Scheduling
# ==============================================================================

class ScheduleDebatesView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = "draw_set_debate_times.html"

    def get_context_data(self, **kwargs):
        round = self.get_round()
        kwargs['venue_groups'] = Team.objects.filter(tournament=round.tournament).count()
        kwargs['divisions'] = Division.objects.filter(tournament=round.tournament).order_by('id')
        return super().get_context_data(**kwargs)


class ScheduleConfirmationsView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = "confirmations_view.html"

    def get_context_data(self, **kwargs):
        adjs = Adjudicator.objects.all().order_by('name')
        for adj in adjs:
            shifts = DebateAdjudicator.objects.filter(adjudicator=adj, debate__round__tournament__active=True)
            if len(shifts) > 0:
                adj.shifts = shifts
        kwargs['adjs'] = adjs


class ApplyDebateSchedyleView(DrawStatusEdit):

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        debates = Debate.objects.filter(round=round)
        for debate in debates:
            division = debate.teams[0].division
            if division and division.time_slot:
                date = request.POST[str(division.venue_group.id)]
                if date:
                    time = "%s %s" % (date, division.time_slot)
                    try:
                        debate.time = datetime.datetime.strptime(
                            time, "%Y-%m-%d %H:%M:%S")  # Chrome
                    except ValueError:
                        debate.time = datetime.datetime.strptime(
                            time, "%d/%m/%Y %H:%M:%S")  # Others

                    debate.save()

        messages.success(self.request, "Applied schedules to debates")
        return super().post(request, *args, **kwargs)


# ==============================================================================
# Sides Editing and Viewing
# ==============================================================================

class BaseSideAllocationsView(TournamentMixin, VueTableMixin):

    page_title = "Side Pre-Allocations"

    def get_table(self):
        tournament = self.get_tournament()
        teams = tournament.team_set.all()
        rounds = tournament.prelim_rounds()

        tpas = dict()
        for tpa in TeamPositionAllocation.objects.filter(round__in=rounds):
            tpas[(tpa.team.id, tpa.round.seq)] = TPA_MAP[tpa.position]

        table = TabbycatTableBuilder(view=self)
        table.add_team_columns(teams)

        headers = [round.abbreviation for round in rounds]
        data = [[tpas.get((team.id, round.id), "—") for round in rounds] for team in teams]
        table.add_columns(headers, data)

        return table


class SideAllocationsView(SuperuserRequiredMixin, BaseSideAllocationsView):
    pass


class PublicSideAllocationsView(PublicTournamentPageMixin, BaseSideAllocationsView):
    public_page_preference = 'public_side_allocations'


class DrawMatchupsEditView(SuperuserRequiredMixin, RoundMixin, TemplateView):
    template_name = 'draw_matchups_edit.html'

    def get_context_data(self, **kwargs):
        round = self.get_round()
        kwargs['standings'], kwargs['draw'] = get_draw_with_standings(round)
        kwargs['unused_teams'] = round.unused_teams()
        possible_debates = len(kwargs['unused_teams']) // 2 + 1
        kwargs['possible_debates'] = [None] * possible_debates

        return super().get_context_data(**kwargs)


class SaveDrawMatchups(SuperuserRequiredMixin, RoundMixin, ExpectPost):

    def dispatch(self, request, *args, **kwargs):
        existing_debate_ids = [int(a.replace('debate_', ''))
                               for a in list(request.POST.keys())
                               if a.startswith('debate_')]
        for debate_id in existing_debate_ids:
            debate = Debate.objects.get(id=debate_id)
            new_aff_id = request.POST.get('aff_%s' % debate_id).replace('team_',
                                                                        '')
            new_neg_id = request.POST.get('neg_%s' % debate_id).replace('team_',
                                                                        '')

            if new_aff_id and new_neg_id:
                DebateTeam.objects.filter(debate=debate).delete()
                debate.save()

                new_aff_team = Team.objects.get(id=int(new_aff_id))
                new_aff_dt = DebateTeam(debate=debate,
                                        team=new_aff_team,
                                        position=DebateTeam.POSITION_AFFIRMATIVE)
                new_aff_dt.save()

                new_aff_team = Team.objects.get(id=int(new_neg_id))
                new_neg_dt = DebateTeam(debate=debate,
                                        team=new_aff_team,
                                        position=DebateTeam.POSITION_NEGATIVE)
                new_neg_dt.save()
            else:
                # If there's blank debates we need to delete those
                debate.delete()

        new_debate_ids = [int(a.replace('new_debate_', ''))
                          for a in list(request.POST.keys())
                          if a.startswith('new_debate_')]
        for debate_id in new_debate_ids:
            new_aff_id = request.POST.get('aff_%s' % debate_id).replace('team_',
                                                                        '')
            new_neg_id = request.POST.get('neg_%s' % debate_id).replace('team_',
                                                                        '')

            if new_aff_id and new_neg_id:
                debate = Debate(round=round, venue=None)
                debate.save()

                aff_team = Team.objects.get(id=int(new_aff_id))
                neg_team = Team.objects.get(id=int(new_neg_id))
                new_aff_dt = DebateTeam(debate=debate,
                                        team=aff_team,
                                        position=DebateTeam.POSITION_AFFIRMATIVE)
                new_neg_dt = DebateTeam(debate=debate,
                                        team=neg_team,
                                        position=DebateTeam.POSITION_NEGATIVE)
                new_aff_dt.save()
                new_neg_dt.save()

        return HttpResponse("ok")
