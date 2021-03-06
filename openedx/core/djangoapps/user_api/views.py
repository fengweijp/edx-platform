"""HTTP end-points for the User API. """
import copy

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import NON_FIELD_ERRORS, ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseForbidden
from django.utils.decorators import method_decorator
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt, csrf_protect, ensure_csrf_cookie
from django.views.decorators.debug import sensitive_post_parameters
from django_countries import countries
from django_filters.rest_framework import DjangoFilterBackend
from opaque_keys import InvalidKeyError
from opaque_keys.edx import locator
from opaque_keys.edx.locations import SlashSeparatedCourseKey
from rest_framework import authentication, generics, status, viewsets
from rest_framework.exceptions import ParseError
from rest_framework.views import APIView

import third_party_auth
from django_comment_common.models import Role
from edxmako.shortcuts import marketing_link
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.user_api.accounts.api import check_account_exists
from openedx.core.lib.api.authentication import SessionAuthenticationAllowInactiveUser
from openedx.core.lib.api.permissions import ApiKeyHeaderPermission
from openedx.features.enterprise_support.api import enterprise_customer_for_request
from student.cookies import set_logged_in_cookies
from student.forms import get_registration_extension_form
from student.views import create_account_with_params, AccountValidationError
from util.json_request import JsonResponse

import accounts
from .helpers import FormDescription, require_post_params, shim_student_view
from .models import UserPreference, UserProfile
from .preferences.api import get_country_time_zones, update_email_opt_in
from .serializers import CountryTimeZoneSerializer, UserPreferenceSerializer, UserSerializer


class LoginSessionView(APIView):
    """HTTP end-points for logging in users. """

    # This end-point is available to anonymous users,
    # so do not require authentication.
    authentication_classes = []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        """Return a description of the login form.

        This decouples clients from the API definition:
        if the API decides to modify the form, clients won't need
        to be updated.

        See `user_api.helpers.FormDescription` for examples
        of the JSON-encoded form description.

        Returns:
            HttpResponse

        """
        form_desc = FormDescription("post", reverse("user_api_login_session"))

        # Translators: This label appears above a field on the login form
        # meant to hold the user's email address.
        email_label = _(u"Email")

        # Translators: This example email address is used as a placeholder in
        # a field on the login form meant to hold the user's email address.
        email_placeholder = _(u"username@domain.com")

        # Translators: These instructions appear on the login form, immediately
        # below a field meant to hold the user's email address.
        email_instructions = _("The email address you used to register with {platform_name}").format(
            platform_name=configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME)
        )

        form_desc.add_field(
            "email",
            field_type="email",
            label=email_label,
            placeholder=email_placeholder,
            instructions=email_instructions,
            restrictions={
                "min_length": accounts.EMAIL_MIN_LENGTH,
                "max_length": accounts.EMAIL_MAX_LENGTH,
            }
        )

        # Translators: This label appears above a field on the login form
        # meant to hold the user's password.
        password_label = _(u"Password")

        form_desc.add_field(
            "password",
            label=password_label,
            field_type="password",
            restrictions={
                "min_length": accounts.PASSWORD_MIN_LENGTH,
                "max_length": accounts.PASSWORD_MAX_LENGTH,
            }
        )

        form_desc.add_field(
            "remember",
            field_type="checkbox",
            label=_("Remember me"),
            default=False,
            required=False,
        )

        return HttpResponse(form_desc.to_json(), content_type="application/json")

    @method_decorator(require_post_params(["email", "password"]))
    @method_decorator(csrf_protect)
    def post(self, request):
        """Log in a user.

        You must send all required form fields with the request.

        You can optionally send an `analytics` param with a JSON-encoded
        object with additional info to include in the login analytics event.
        Currently, the only supported field is "enroll_course_id" to indicate
        that the user logged in while enrolling in a particular course.

        Arguments:
            request (HttpRequest)

        Returns:
            HttpResponse: 200 on success
            HttpResponse: 400 if the request is not valid.
            HttpResponse: 403 if authentication failed.
                403 with content "third-party-auth" if the user
                has successfully authenticated with a third party provider
                but does not have a linked account.
            HttpResponse: 302 if redirecting to another page.

        Example Usage:

            POST /user_api/v1/login_session
            with POST params `email`, `password`, and `remember`.

            200 OK

        """
        # For the initial implementation, shim the existing login view
        # from the student Django app.
        from student.views import login_user
        return shim_student_view(login_user, check_logged_in=True)(request)

    @method_decorator(sensitive_post_parameters("password"))
    def dispatch(self, request, *args, **kwargs):
        return super(LoginSessionView, self).dispatch(request, *args, **kwargs)


class RegistrationView(APIView):
    """HTTP end-points for creating a new user. """

    DEFAULT_FIELDS = ["email", "name", "username", "password"]

    EXTRA_FIELDS = [
        "confirm_email",
        "first_name",
        "last_name",
        "city",
        "state",
        "country",
        "gender",
        "year_of_birth",
        "level_of_education",
        "company",
        "title",
        "mailing_address",
        "goals",
        "honor_code",
        "terms_of_service",
    ]

    # This end-point is available to anonymous users,
    # so do not require authentication.
    authentication_classes = []

    def _is_field_visible(self, field_name):
        """Check whether a field is visible based on Django settings. """
        return self._extra_fields_setting.get(field_name) in ["required", "optional"]

    def _is_field_required(self, field_name):
        """Check whether a field is required based on Django settings. """
        return self._extra_fields_setting.get(field_name) == "required"

    def __init__(self, *args, **kwargs):
        super(RegistrationView, self).__init__(*args, **kwargs)

        # Backwards compatibility: Honor code is required by default, unless
        # explicitly set to "optional" in Django settings.
        self._extra_fields_setting = copy.deepcopy(configuration_helpers.get_value('REGISTRATION_EXTRA_FIELDS'))
        if not self._extra_fields_setting:
            self._extra_fields_setting = copy.deepcopy(settings.REGISTRATION_EXTRA_FIELDS)
        self._extra_fields_setting["honor_code"] = self._extra_fields_setting.get("honor_code", "required")

        # Check that the setting is configured correctly
        for field_name in self.EXTRA_FIELDS:
            if self._extra_fields_setting.get(field_name, "hidden") not in ["required", "optional", "hidden"]:
                msg = u"Setting REGISTRATION_EXTRA_FIELDS values must be either required, optional, or hidden."
                raise ImproperlyConfigured(msg)

        # Map field names to the instance method used to add the field to the form
        self.field_handlers = {}
        valid_fields = self.DEFAULT_FIELDS + self.EXTRA_FIELDS
        for field_name in valid_fields:
            handler = getattr(self, "_add_{field_name}_field".format(field_name=field_name))
            self.field_handlers[field_name] = handler

        field_order = configuration_helpers.get_value('REGISTRATION_FIELD_ORDER')
        if not field_order:
            field_order = settings.REGISTRATION_FIELD_ORDER or valid_fields

        # Check that all of the valid_fields are in the field order and vice versa, if not set to the default order
        if set(valid_fields) != set(field_order):
            field_order = valid_fields

        self.field_order = field_order

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        """Return a description of the registration form.

        This decouples clients from the API definition:
        if the API decides to modify the form, clients won't need
        to be updated.

        This is especially important for the registration form,
        since different edx-platform installations might
        collect different demographic information.

        See `user_api.helpers.FormDescription` for examples
        of the JSON-encoded form description.

        Arguments:
            request (HttpRequest)

        Returns:
            HttpResponse

        """
        form_desc = FormDescription("post", reverse("user_api_registration"))
        self._apply_third_party_auth_overrides(request, form_desc)

        # Custom form fields can be added via the form set in settings.REGISTRATION_EXTENSION_FORM
        custom_form = get_registration_extension_form()

        if custom_form:
            # Default fields are always required
            for field_name in self.DEFAULT_FIELDS:
                self.field_handlers[field_name](form_desc, required=True)

            for field_name, field in custom_form.fields.items():
                restrictions = {}
                if getattr(field, 'max_length', None):
                    restrictions['max_length'] = field.max_length
                if getattr(field, 'min_length', None):
                    restrictions['min_length'] = field.min_length
                field_options = getattr(
                    getattr(custom_form, 'Meta', None), 'serialization_options', {}
                ).get(field_name, {})
                field_type = field_options.get('field_type', FormDescription.FIELD_TYPE_MAP.get(field.__class__))
                if not field_type:
                    raise ImproperlyConfigured(
                        "Field type '{}' not recognized for registration extension field '{}'.".format(
                            field_type,
                            field_name
                        )
                    )
                form_desc.add_field(
                    field_name, label=field.label,
                    default=field_options.get('default'),
                    field_type=field_options.get('field_type', FormDescription.FIELD_TYPE_MAP.get(field.__class__)),
                    placeholder=field.initial, instructions=field.help_text, required=field.required,
                    restrictions=restrictions,
                    options=getattr(field, 'choices', None), error_messages=field.error_messages,
                    include_default_option=field_options.get('include_default_option'),
                )

            # Extra fields configured in Django settings
            # may be required, optional, or hidden
            for field_name in self.EXTRA_FIELDS:
                if self._is_field_visible(field_name):
                    self.field_handlers[field_name](
                        form_desc,
                        required=self._is_field_required(field_name)
                    )
        else:
            # Go through the fields in the fields order and add them if they are required or visible
            for field_name in self.field_order:
                if field_name in self.DEFAULT_FIELDS:
                    self.field_handlers[field_name](form_desc, required=True)
                elif self._is_field_visible(field_name):
                    self.field_handlers[field_name](
                        form_desc,
                        required=self._is_field_required(field_name)
                    )

        return HttpResponse(form_desc.to_json(), content_type="application/json")

    @method_decorator(csrf_exempt)
    def post(self, request):
        """Create the user's account.

        You must send all required form fields with the request.

        You can optionally send a "course_id" param to indicate in analytics
        events that the user registered while enrolling in a particular course.

        Arguments:
            request (HTTPRequest)

        Returns:
            HttpResponse: 200 on success
            HttpResponse: 400 if the request is not valid.
            HttpResponse: 409 if an account with the given username or email
                address already exists
            HttpResponse: 403 operation not allowed
        """
        data = request.POST.copy()

        email = data.get('email')
        username = data.get('username')

        # Handle duplicate email/username
        conflicts = check_account_exists(email=email, username=username)
        if conflicts:
            conflict_messages = {
                "email": accounts.EMAIL_CONFLICT_MSG.format(email_address=email),
                "username": accounts.USERNAME_CONFLICT_MSG.format(username=username),
            }
            errors = {
                field: [{"user_message": conflict_messages[field]}]
                for field in conflicts
            }
            return JsonResponse(errors, status=409)

        # Backwards compatibility: the student view expects both
        # terms of service and honor code values.  Since we're combining
        # these into a single checkbox, the only value we may get
        # from the new view is "honor_code".
        # Longer term, we will need to make this more flexible to support
        # open source installations that may have separate checkboxes
        # for TOS, privacy policy, etc.
        if data.get("honor_code") and "terms_of_service" not in data:
            data["terms_of_service"] = data["honor_code"]

        try:
            user = create_account_with_params(request, data)
        except AccountValidationError as err:
            errors = {
                err.field: [{"user_message": err.message}]
            }
            return JsonResponse(errors, status=409)
        except ValidationError as err:
            # Should only get non-field errors from this function
            assert NON_FIELD_ERRORS not in err.message_dict
            # Only return first error for each field
            errors = {
                field: [{"user_message": error} for error in error_list]
                for field, error_list in err.message_dict.items()
            }
            return JsonResponse(errors, status=400)
        except PermissionDenied:
            return HttpResponseForbidden(_("Account creation not allowed."))

        response = JsonResponse({"success": True})
        set_logged_in_cookies(request, response, user)
        return response

    @method_decorator(sensitive_post_parameters("password"))
    def dispatch(self, request, *args, **kwargs):
        return super(RegistrationView, self).dispatch(request, *args, **kwargs)

    def _add_email_field(self, form_desc, required=True):
        """Add an email field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to hold the user's email address.
        email_label = _(u"Email")

        # Translators: This example email address is used as a placeholder in
        # a field on the registration form meant to hold the user's email address.
        email_placeholder = _(u"username@domain.com")

        # Translators: These instructions appear on the registration form, immediately
        # below a field meant to hold the user's email address.
        email_instructions = _(u"This is what you will use to login.")

        form_desc.add_field(
            "email",
            field_type="email",
            label=email_label,
            placeholder=email_placeholder,
            instructions=email_instructions,
            restrictions={
                "min_length": accounts.EMAIL_MIN_LENGTH,
                "max_length": accounts.EMAIL_MAX_LENGTH,
            },
            required=required
        )

    def _add_confirm_email_field(self, form_desc, required=True):
        """Add an email confirmation field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to confirm the user's email address.
        email_label = _(u"Confirm Email")
        error_msg = accounts.REQUIRED_FIELD_CONFIRM_EMAIL_MSG

        form_desc.add_field(
            "confirm_email",
            label=email_label,
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_name_field(self, form_desc, required=True):
        """Add a name field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to hold the user's full name.
        name_label = _(u"Full Name")

        # Translators: This example name is used as a placeholder in
        # a field on the registration form meant to hold the user's name.
        name_placeholder = _(u"Jane Q. Learner")

        # Translators: These instructions appear on the registration form, immediately
        # below a field meant to hold the user's full name.
        name_instructions = _(u"This name will be used on any certificates that you earn.")

        form_desc.add_field(
            "name",
            label=name_label,
            placeholder=name_placeholder,
            instructions=name_instructions,
            restrictions={
                "max_length": accounts.NAME_MAX_LENGTH,
            },
            required=required
        )

    def _add_username_field(self, form_desc, required=True):
        """Add a username field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to hold the user's public username.
        username_label = _(u"Public Username")

        username_instructions = _(
            # Translators: These instructions appear on the registration form, immediately
            # below a field meant to hold the user's public username.
            u"The name that will identify you in your courses. "
            u"It cannot be changed later."
        )

        # Translators: This example username is used as a placeholder in
        # a field on the registration form meant to hold the user's username.
        username_placeholder = _(u"Jane_Q_Learner")

        form_desc.add_field(
            "username",
            label=username_label,
            instructions=username_instructions,
            placeholder=username_placeholder,
            restrictions={
                "min_length": accounts.USERNAME_MIN_LENGTH,
                "max_length": accounts.USERNAME_MAX_LENGTH,
            },
            required=required
        )

    def _add_password_field(self, form_desc, required=True):
        """Add a password field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to hold the user's password.
        password_label = _(u"Password")

        form_desc.add_field(
            "password",
            label=password_label,
            field_type="password",
            restrictions={
                "min_length": accounts.PASSWORD_MIN_LENGTH,
                "max_length": accounts.PASSWORD_MAX_LENGTH,
            },
            required=required
        )

    def _add_level_of_education_field(self, form_desc, required=True):
        """Add a level of education field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a dropdown menu on the registration
        # form used to select the user's highest completed level of education.
        education_level_label = _(u"Highest level of education completed")
        error_msg = accounts.REQUIRED_FIELD_LEVEL_OF_EDUCATION_MSG

        # The labels are marked for translation in UserProfile model definition.
        options = [(name, _(label)) for name, label in UserProfile.LEVEL_OF_EDUCATION_CHOICES]  # pylint: disable=translation-of-non-string
        form_desc.add_field(
            "level_of_education",
            label=education_level_label,
            field_type="select",
            options=options,
            include_default_option=True,
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_gender_field(self, form_desc, required=True):
        """Add a gender field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a dropdown menu on the registration
        # form used to select the user's gender.
        gender_label = _(u"Gender")

        # The labels are marked for translation in UserProfile model definition.
        options = [(name, _(label)) for name, label in UserProfile.GENDER_CHOICES]  # pylint: disable=translation-of-non-string
        form_desc.add_field(
            "gender",
            label=gender_label,
            field_type="select",
            options=options,
            include_default_option=True,
            required=required
        )

    def _add_year_of_birth_field(self, form_desc, required=True):
        """Add a year of birth field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a dropdown menu on the registration
        # form used to select the user's year of birth.
        yob_label = _(u"Year of birth")

        options = [(unicode(year), unicode(year)) for year in UserProfile.VALID_YEARS]
        form_desc.add_field(
            "year_of_birth",
            label=yob_label,
            field_type="select",
            options=options,
            include_default_option=True,
            required=required
        )

    def _add_mailing_address_field(self, form_desc, required=True):
        """Add a mailing address field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # meant to hold the user's mailing address.
        mailing_address_label = _(u"Mailing address")
        error_msg = accounts.REQUIRED_FIELD_MAILING_ADDRESS_MSG

        form_desc.add_field(
            "mailing_address",
            label=mailing_address_label,
            field_type="textarea",
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_goals_field(self, form_desc, required=True):
        """Add a goals field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This phrase appears above a field on the registration form
        # meant to hold the user's reasons for registering with edX.
        goals_label = _(u"Tell us why you're interested in {platform_name}").format(
            platform_name=configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME)
        )
        error_msg = accounts.REQUIRED_FIELD_GOALS_MSG

        form_desc.add_field(
            "goals",
            label=goals_label,
            field_type="textarea",
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_city_field(self, form_desc, required=True):
        """Add a city field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the city in which they live.
        city_label = _(u"City")
        error_msg = accounts.REQUIRED_FIELD_CITY_MSG

        form_desc.add_field(
            "city",
            label=city_label,
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_state_field(self, form_desc, required=False):
        """Add a State/Province/Region field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to False

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the State/Province/Region in which they live.
        state_label = _(u"State/Province/Region")

        form_desc.add_field(
            "state",
            label=state_label,
            required=required
        )

    def _add_company_field(self, form_desc, required=False):
        """Add a Company field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to False

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the Company
        company_label = _(u"Company")

        form_desc.add_field(
            "company",
            label=company_label,
            required=required
        )

    def _add_title_field(self, form_desc, required=False):
        """Add a Title field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to False

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the Title
        title_label = _(u"Title")

        form_desc.add_field(
            "title",
            label=title_label,
            required=required
        )

    def _add_first_name_field(self, form_desc, required=False):
        """Add a First Name field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to False

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the First Name
        first_name_label = _(u"First Name")

        form_desc.add_field(
            "first_name",
            label=first_name_label,
            required=required
        )

    def _add_last_name_field(self, form_desc, required=False):
        """Add a Last Name field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to False

        """
        # Translators: This label appears above a field on the registration form
        # which allows the user to input the First Name
        last_name_label = _(u"Last Name")

        form_desc.add_field(
            "last_name",
            label=last_name_label,
            required=required
        )

    def _add_country_field(self, form_desc, required=True):
        """Add a country field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This label appears above a dropdown menu on the registration
        # form used to select the country in which the user lives.
        country_label = _(u"Country")
        error_msg = accounts.REQUIRED_FIELD_COUNTRY_MSG

        # If we set a country code, make sure it's uppercase for the sake of the form.
        default_country = form_desc._field_overrides.get('country', {}).get('defaultValue')
        if default_country:
            form_desc.override_field_properties(
                'country',
                default=default_country.upper()
            )

        form_desc.add_field(
            "country",
            label=country_label,
            field_type="select",
            options=list(countries),
            include_default_option=True,
            required=required,
            error_messages={
                "required": error_msg
            }
        )

    def _add_honor_code_field(self, form_desc, required=True):
        """Add an honor code field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Separate terms of service and honor code checkboxes
        if self._is_field_visible("terms_of_service"):
            terms_label = _(u"Honor Code")
            terms_link = marketing_link("HONOR")
            terms_text = _(u"Review the Honor Code")

        # Combine terms of service and honor code checkboxes
        else:
            # Translators: This is a legal document users must agree to
            # in order to register a new account.
            terms_label = _(u"Terms of Service and Honor Code")
            terms_link = marketing_link("HONOR")
            terms_text = _(u"Review the Terms of Service and Honor Code")

        # Translators: "Terms of Service" is a legal document users must agree to
        # in order to register a new account.
        label = _(u"I agree to the {platform_name} {terms_of_service}").format(
            platform_name=configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME),
            terms_of_service=terms_label
        )

        # Translators: "Terms of Service" is a legal document users must agree to
        # in order to register a new account.
        error_msg = _(u"You must agree to the {platform_name} {terms_of_service}").format(
            platform_name=configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME),
            terms_of_service=terms_label
        )

        form_desc.add_field(
            "honor_code",
            label=label,
            field_type="checkbox",
            default=False,
            required=required,
            error_messages={
                "required": error_msg
            },
            supplementalLink=terms_link,
            supplementalText=terms_text
        )

    def _add_terms_of_service_field(self, form_desc, required=True):
        """Add a terms of service field to a form description.

        Arguments:
            form_desc: A form description

        Keyword Arguments:
            required (bool): Whether this field is required; defaults to True

        """
        # Translators: This is a legal document users must agree to
        # in order to register a new account.
        terms_label = _(u"Terms of Service")
        terms_link = marketing_link("TOS")
        terms_text = _(u"Review the Terms of Service")

        # Translators: "Terms of service" is a legal document users must agree to
        # in order to register a new account.
        label = _(u"I agree to the {platform_name} {terms_of_service}").format(
            platform_name=configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME),
            terms_of_service=terms_label
        )

        # Translators: "Terms of service" is a legal document users must agree to
        # in order to register a new account.
        error_msg = _(u"You must agree to the {platform_name} {terms_of_service}").format(
            platform_name=configuration_helpers.get_value("PLATFORM_NAME", settings.PLATFORM_NAME),
            terms_of_service=terms_label
        )

        form_desc.add_field(
            "terms_of_service",
            label=label,
            field_type="checkbox",
            default=False,
            required=required,
            error_messages={
                "required": error_msg
            },
            supplementalLink=terms_link,
            supplementalText=terms_text
        )

    def _apply_third_party_auth_overrides(self, request, form_desc):
        """Modify the registration form if the user has authenticated with a third-party provider.

        If a user has successfully authenticated with a third-party provider,
        but does not yet have an account with EdX, we want to fill in
        the registration form with any info that we get from the
        provider.

        This will also hide the password field, since we assign users a default
        (random) password on the assumption that they will be using
        third-party auth to log in.

        Arguments:
            request (HttpRequest): The request for the registration form, used
                to determine if the user has successfully authenticated
                with a third-party provider.

            form_desc (FormDescription): The registration form description

        """
        if third_party_auth.is_enabled():
            running_pipeline = third_party_auth.pipeline.get(request)
            if running_pipeline:
                current_provider = third_party_auth.provider.Registry.get_from_pipeline(running_pipeline)

                if current_provider:
                    # Override username / email / full name
                    field_overrides = current_provider.get_register_form_data(
                        running_pipeline.get('kwargs')
                    )

                    # When the TPA Provider is configured to skip the registration form and we are in an
                    # enterprise context, we need to hide all fields except for terms of service and
                    # ensure that the user explicitly checks that field.
                    hide_registration_fields_except_tos = (current_provider.skip_registration_form and
                                                           enterprise_customer_for_request(request))

                    for field_name in self.DEFAULT_FIELDS + self.EXTRA_FIELDS:
                        if field_name in field_overrides:
                            form_desc.override_field_properties(
                                field_name, default=field_overrides[field_name]
                            )

                            if (field_name not in ['terms_of_service', 'honor_code']
                                    and field_overrides[field_name]
                                    and hide_registration_fields_except_tos):

                                form_desc.override_field_properties(
                                    field_name,
                                    field_type="hidden",
                                    label="",
                                    instructions="",
                                )

                    # Hide the password field
                    form_desc.override_field_properties(
                        "password",
                        default="",
                        field_type="hidden",
                        required=False,
                        label="",
                        instructions="",
                        restrictions={}
                    )
                    # used to identify that request is running third party social auth
                    form_desc.add_field(
                        "social_auth_provider",
                        field_type="hidden",
                        label="",
                        default=current_provider.name if current_provider.name else "Third Party",
                        required=False,
                    )


class PasswordResetView(APIView):
    """HTTP end-point for GETting a description of the password reset form. """

    # This end-point is available to anonymous users,
    # so do not require authentication.
    authentication_classes = []

    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        """Return a description of the password reset form.

        This decouples clients from the API definition:
        if the API decides to modify the form, clients won't need
        to be updated.

        See `user_api.helpers.FormDescription` for examples
        of the JSON-encoded form description.

        Returns:
            HttpResponse

        """
        form_desc = FormDescription("post", reverse("password_change_request"))

        # Translators: This label appears above a field on the password reset
        # form meant to hold the user's email address.
        email_label = _(u"Email")

        # Translators: This example email address is used as a placeholder in
        # a field on the password reset form meant to hold the user's email address.
        email_placeholder = _(u"username@domain.com")

        # Translators: These instructions appear on the password reset form,
        # immediately below a field meant to hold the user's email address.
        email_instructions = _(u"The email address you used to register with {platform_name}").format(
            platform_name=configuration_helpers.get_value('PLATFORM_NAME', settings.PLATFORM_NAME)
        )

        form_desc.add_field(
            "email",
            field_type="email",
            label=email_label,
            placeholder=email_placeholder,
            instructions=email_instructions,
            restrictions={
                "min_length": accounts.EMAIL_MIN_LENGTH,
                "max_length": accounts.EMAIL_MAX_LENGTH,
            }
        )

        return HttpResponse(form_desc.to_json(), content_type="application/json")


class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    DRF class for interacting with the User ORM object
    """
    authentication_classes = (authentication.SessionAuthentication,)
    permission_classes = (ApiKeyHeaderPermission,)
    queryset = User.objects.all().prefetch_related("preferences").select_related("profile")
    serializer_class = UserSerializer
    paginate_by = 10
    paginate_by_param = "page_size"


class ForumRoleUsersListView(generics.ListAPIView):
    """
    Forum roles are represented by a list of user dicts
    """
    authentication_classes = (authentication.SessionAuthentication,)
    permission_classes = (ApiKeyHeaderPermission,)
    serializer_class = UserSerializer
    paginate_by = 10
    paginate_by_param = "page_size"

    def get_queryset(self):
        """
        Return a list of users with the specified role/course pair
        """
        name = self.kwargs['name']
        course_id_string = self.request.query_params.get('course_id')
        if not course_id_string:
            raise ParseError('course_id must be specified')
        course_id = SlashSeparatedCourseKey.from_deprecated_string(course_id_string)
        role = Role.objects.get_or_create(course_id=course_id, name=name)[0]
        users = role.users.prefetch_related("preferences").select_related("profile").all()
        return users


class UserPreferenceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    DRF class for interacting with the UserPreference ORM
    """
    authentication_classes = (authentication.SessionAuthentication,)
    permission_classes = (ApiKeyHeaderPermission,)
    queryset = UserPreference.objects.all()
    filter_backends = (DjangoFilterBackend,)
    filter_fields = ("key", "user")
    serializer_class = UserPreferenceSerializer
    paginate_by = 10
    paginate_by_param = "page_size"


class PreferenceUsersListView(generics.ListAPIView):
    """
    DRF class for listing a user's preferences
    """
    authentication_classes = (authentication.SessionAuthentication,)
    permission_classes = (ApiKeyHeaderPermission,)
    serializer_class = UserSerializer
    paginate_by = 10
    paginate_by_param = "page_size"

    def get_queryset(self):
        return User.objects.filter(
            preferences__key=self.kwargs["pref_key"]
        ).prefetch_related("preferences").select_related("profile")


class UpdateEmailOptInPreference(APIView):
    """View for updating the email opt in preference. """
    authentication_classes = (SessionAuthenticationAllowInactiveUser,)

    @method_decorator(require_post_params(["course_id", "email_opt_in"]))
    @method_decorator(ensure_csrf_cookie)
    def post(self, request):
        """ Post function for updating the email opt in preference.

        Allows the modification or creation of the email opt in preference at an
        organizational level.

        Args:
            request (Request): The request should contain the following POST parameters:
                * course_id: The slash separated course ID. Used to determine the organization
                    for this preference setting.
                * email_opt_in: "True" or "False" to determine if the user is opting in for emails from
                    this organization. If the string does not match "True" (case insensitive) it will
                    assume False.

        """
        course_id = request.data['course_id']
        try:
            org = locator.CourseLocator.from_string(course_id).org
        except InvalidKeyError:
            return HttpResponse(
                status=400,
                content="No course '{course_id}' found".format(course_id=course_id),
                content_type="text/plain"
            )
        # Only check for true. All other values are False.
        email_opt_in = request.data['email_opt_in'].lower() == 'true'
        update_email_opt_in(request.user, org, email_opt_in)
        return HttpResponse(status=status.HTTP_200_OK)


class CountryTimeZoneListView(generics.ListAPIView):
    """
    **Use Cases**

        Retrieves a list of all time zones, by default, or common time zones for country, if given

        The country is passed in as its ISO 3166-1 Alpha-2 country code as an
        optional 'country_code' argument. The country code is also case-insensitive.

    **Example Requests**

        GET /user_api/v1/preferences/time_zones/

        GET /user_api/v1/preferences/time_zones/?country_code=FR

    **Example GET Response**

        If the request is successful, an HTTP 200 "OK" response is returned along with a
        list of time zone dictionaries for all time zones or just for time zones commonly
        used in a country, if given.

        Each time zone dictionary contains the following values.

            * time_zone: The name of the time zone.
            * description: The display version of the time zone
    """
    serializer_class = CountryTimeZoneSerializer
    paginator = None

    def get_queryset(self):
        country_code = self.request.GET.get('country_code', None)
        return get_country_time_zones(country_code)
