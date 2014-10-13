from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django.contrib.auth.models import User
from mezzanine.generic.models import Rating, ThreadedComment
from theme.models import UserProfile # fixme switch to party model
from hs_core import hydroshare
from collections import Counter

class HydroshareSiteMetrics(TemplateView):
    template_name = 'hs_metrics/hydrosharesitemetrics.html'

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super(HydroshareSiteMetrics, self).dispatch(request, *args, **kwargs)

    def __init__(self, **kwargs):
        super(HydroshareSiteMetrics, self).__init__(**kwargs)

        self.n_registered_users = User.objects.all().count()
        self.n_host_institutions = 0
        self.host_institutions = set()
        self.n_users_logged_on = None # fixme need to track
        self.max_logon_duration = None # fixme need to track
        self.n_courses = 0
        self.n_agencies = 0
        self.agencies = set()
        self.n_core_contributors = 6 # fixme need to track (use GItHub API Key) https://api.github.com/teams/328946
        self.n_extension_contributors = 10 # fixme need to track (use GitHub API Key) https://api.github.com/teams/964835
        self.n_citations = 0 # fixme hard to quantify
        self.resource_type_counts = Counter()
        self.user_titles = Counter()
        self.user_professions = Counter()
        self.user_subject_areas = Counter()
        self.n_ratings = 0
        self.n_comments = 0
        self.n_resources = 0

    def get_context_data(self, **kwargs):
        """
        1.	Number of registered users (with voluntarily supplied demography and diversity)
        2.	Number of host institutions (with demography).
        3.	Use statistics (for each month number and average log-on duration, maximum number of users logged on, total
            CPU hours of model run time by different compute resources).
        4.	Number of courses and students using educational material (with demography and diversity based on user
            information).
        5.	Number of ratings and comments about resources.
        6.	The quantity of hydrological data including data values, sites, and variables, and web service data requests
            per day.
        7.	The number of non-CUAHSI agencies that utilize HydroShare (e.g. NCDC).
        8.	The number of contributors to the core infrastructure code base.
        9.	The number of contributors to non-core code that is part of the system, such as clients or apps and other
            software projects where changes are made to adapt for HydroShare
        10.	The number of downloads of releases of clients and apps.
        11.	The number of users trained during the various outreach activities.
        12.	Number of papers submitted to and published in peer reviewed forums about this project or using the
            infrastructure of this project.  To the extent possible these will be stratified demographically and based
            on whether they report contributions that are domain research or cyberinfrastructure.  We will also measure
            posters, invited talks, panel sessions, etc. We will also track citations generated by these papers.
        13.	Number of citations of various HydroShare resources.
        14.	The types and amounts of resources stored within the system, and their associated downloads (resource types
            will include data of varying type, model codes, scripts, workflows and documents).

        :param kwargs:
        :return:
        """

        ctx = super(HydroshareSiteMetrics, self).get_context_data(**kwargs)
        self.get_resource_stats()
        self.get_user_stats()
        self.user_professions = self.user_professions.items()
        self.user_subject_areas = self.user_subject_areas.items()
        self.resource_type_counts = self.resource_type_counts.items()
        self.user_titles = self.user_titles.items()
        ctx['metrics'] = self
        return ctx

    def get_all_resources(self):
        """Yield all resources in the system as a single generator"""

        resource_types = hydroshare.get_resource_types()
        for qs in (res_model.objects.all() for res_model in resource_types):
            for resource in qs:
                yield resource

    def get_resource_stats(self):
        for resource in self.get_all_resources():
            resource_type_name = resource._meta.verbose_name if hasattr(resource._meta, 'verbose_name') else resource._meta.model_name
            self.resource_type_counts[resource_type_name] += 1
            self.n_resources += 1

        self.n_ratings = Rating.objects.all().count()
        self.n_comments = ThreadedComment.objects.all().count()

    def get_user_stats(self):
        # FIXME revisit this with the hs_party application

        for profile in UserProfile.objects.all():
            if profile.organization_type in ('Government','Commercial'):
                self.agencies.add(profile.organization)
            else:
                self.host_institutions.add(profile.organization)

            self.user_professions[profile.profession] += 1
            self.user_titles[profile.title] += 1

            if profile.subject_areas:
                self.user_subject_areas.update(a.strip() for a in profile.subject_areas.split(','))

        self.n_host_institutions = len(self.host_institutions)
        self.n_agencies = len(self.agencies)