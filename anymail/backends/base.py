import json

import requests
# noinspection PyUnresolvedReferences
from six.moves.urllib.parse import urljoin

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

from ..exceptions import AnymailError, AnymailRequestsAPIError, AnymailSerializationError, AnymailUnsupportedFeature
from ..utils import Attachment, ParsedEmail, UNSET, combine, last, get_anymail_setting
from .._version import __version__


class AnymailBaseBackend(BaseEmailBackend):
    """
    Base Anymail email backend
    """

    def __init__(self, *args, **kwargs):
        super(AnymailBaseBackend, self).__init__(*args, **kwargs)

        self.unsupported_feature_errors = get_anymail_setting("UNSUPPORTED_FEATURE_ERRORS", True)
        self.ignore_recipient_status = get_anymail_setting("IGNORE_RECIPIENT_STATUS", False)

        # Merge SEND_DEFAULTS and <esp_name>_SEND_DEFAULTS settings
        send_defaults = get_anymail_setting("SEND_DEFAULTS", {})
        esp_send_defaults = get_anymail_setting("%s_SEND_DEFAULTS" % self.esp_name.upper(), None)
        if esp_send_defaults is not None:
            send_defaults = send_defaults.copy()
            send_defaults.update(esp_send_defaults)
        self.send_defaults = send_defaults

    def open(self):
        """
        Open and persist a connection to the ESP's API, and whether
        a new connection was created.

        Callers must ensure they later call close, if (and only if) open
        returns True.
        """
        # Subclasses should use an instance property to maintain a cached
        # connection, and return True iff they initialize that instance
        # property in _this_ open call. (If the cached connection already
        # exists, just do nothing and return False.)
        #
        # Subclasses should swallow operational errors if self.fail_silently
        # (e.g., network errors), but otherwise can raise any errors.
        #
        # (Returning a bool to indicate whether connection was created is
        # borrowed from django.core.email.backends.SMTPBackend)
        return False

    def close(self):
        """
        Close the cached connection created by open.

        You must only call close if your code called open and it returned True.
        """
        # Subclasses should tear down the cached connection and clear
        # the instance property.
        #
        # Subclasses should swallow operational errors if self.fail_silently
        # (e.g., network errors), but otherwise can raise any errors.
        pass

    def send_messages(self, email_messages):
        """
        Sends one or more EmailMessage objects and returns the number of email
        messages sent.
        """
        # This API is specified by Django's core BaseEmailBackend
        # (so you can't change it to, e.g., return detailed status).
        # Subclasses shouldn't need to override.

        num_sent = 0
        if not email_messages:
            return num_sent

        created_session = self.open()

        try:
            for message in email_messages:
                try:
                    sent = self._send(message)
                except AnymailError:
                    if self.fail_silently:
                        sent = False
                    else:
                        raise
                if sent:
                    num_sent += 1
        finally:
            if created_session:
                self.close()

        return num_sent

    def _send(self, message):
        """Sends the EmailMessage message, and returns True if the message was sent.

        This should only be called by the base send_messages loop.

        Implementations must raise exceptions derived from AnymailError for
        anticipated failures that should be suppressed in fail_silently mode.
        """
        message.anymail_status = None
        esp_response_attr = "%s_response" % self.esp_name.lower()  # e.g., message.mandrill_response
        setattr(message, esp_response_attr, None)  # until we have a response
        if not message.recipients():
            return False

        payload = self.build_message_payload(message)
        # FUTURE: if pre-send-signal OK...
        response = self.post_to_esp(payload, message)

        parsed_response = self.deserialize_response(response, payload, message)
        setattr(message, esp_response_attr, parsed_response)
        message.anymail_status = self.validate_response(parsed_response, response, payload, message)
        # FUTURE: post-send signal

        return True

    def build_message_payload(self, message):
        """Returns a payload that will allow message to be sent via the ESP.

        Derived classes must implement, and should subclass :class:BasePayload
        to get standard Anymail options.

        Raises :exc:AnymailUnsupportedFeature for message options that
        cannot be communicated to the ESP.

        :param message: :class:EmailMessage
        :return: :class:BasePayload
        """
        raise NotImplementedError("%s.%s must implement build_message_payload" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def post_to_esp(self, payload, message):
        """Post payload to ESP send API endpoint, and return the raw response.

        payload is the result of build_message_payload
        message is the original EmailMessage
        return should be a raw response

        Can raise AnymailAPIError (or derived exception) for problems posting to the ESP
        """
        raise NotImplementedError("%s.%s must implement post_to_esp" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def deserialize_response(self, response, payload, message):
        """Deserialize a raw ESP response

        Can raise AnymailAPIError (or derived exception) if response is unparsable
        """
        raise NotImplementedError("%s.%s must implement deserialize_response" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def validate_response(self, parsed_response, response, payload, message):
        """Validate parsed_response, raising exceptions for any problems, and return normalized status.

        Extend this to provide your own validation checks.
        Validation exceptions should inherit from anymail.exceptions.AnymailError
        for proper fail_silently behavior.

        If *all* recipients are refused or invalid, should raise AnymailRecipientsRefused

        Returns one of "sent", "queued", "refused", "error" or "multi"
        """
        raise NotImplementedError("%s.%s must implement validate_response" %
                                  (self.__class__.__module__, self.__class__.__name__))

    @property
    def esp_name(self):
        """
        Read-only name of the ESP for this backend.

        (E.g., MailgunBackend will return "Mailgun")
        """
        return self.__class__.__name__.replace("Backend", "")


class AnymailRequestsBackend(AnymailBaseBackend):
    """
    Base Anymail email backend for ESPs that use an HTTP API via requests
    """

    def __init__(self, api_url, **kwargs):
        """Init options from Django settings"""
        self.api_url = api_url
        super(AnymailRequestsBackend, self).__init__(**kwargs)
        self.session = None

    def open(self):
        if self.session:
            return False  # already exists

        try:
            self.session = requests.Session()
        except requests.RequestException:
            if not self.fail_silently:
                raise
        else:
            self.session.headers["User-Agent"] = "Anymail/%s %s" % (
                __version__, self.session.headers.get("User-Agent", ""))
            return True

    def close(self):
        if self.session is None:
            return
        try:
            self.session.close()
        except requests.RequestException:
            if not self.fail_silently:
                raise
        finally:
            self.session = None

    def _send(self, message):
        if self.session is None:
            class_name = self.__class__.__name__
            raise RuntimeError(
                "Session has not been opened in {class_name}._send. "
                "(This is either an implementation error in {class_name}, "
                "or you are incorrectly calling _send directly.)".format(class_name=class_name))
        return super(AnymailRequestsBackend, self)._send(message)

    def post_to_esp(self, payload, message):
        """Post payload to ESP send API endpoint, and return the raw response.

        payload is the result of build_message_payload
        message is the original EmailMessage
        return should be a requests.Response

        Can raise AnymailRequestsAPIError for HTTP errors in the post
        """
        params = payload.get_request_params(self.api_url)
        response = self.session.request(**params)
        if response.status_code != 200:
            raise AnymailRequestsAPIError(email_message=message, payload=payload, response=response)
        return response

    def deserialize_response(self, response, payload, message):
        """Return parsed ESP API response

        Can raise AnymailRequestsAPIError if response is unparsable
        """
        try:
            return response.json()
        except ValueError:
            raise AnymailRequestsAPIError("Invalid JSON in %s API response" % self.esp_name,
                                          email_message=message, payload=payload, response=response)


class BasePayload(object):
    # attr, combiner, converter
    base_message_attrs = (
        # Standard EmailMessage/EmailMultiAlternatives props
        ('from_email', last, 'parsed_email'),
        ('to', combine, 'parsed_emails'),
        ('cc', combine, 'parsed_emails'),
        ('bcc', combine, 'parsed_emails'),
        ('subject', last, None),
        ('reply_to', combine, 'parsed_emails'),
        ('extra_headers', combine, None),
        ('body', last, None),  # special handling below checks message.content_subtype
        ('alternatives', combine, None),
        ('attachments', combine, 'prepped_attachments'),
    )
    anymail_message_attrs = (
        # Anymail expando-props
        ('metadata', combine, None),
        ('send_at', last, None),  # normalize to datetime?
        ('tags', combine, None),
        ('track_clicks', last, None),
        ('track_opens', last, None),
        ('esp_extra', combine, None),
    )
    esp_message_attrs = ()  # subclasses can override

    def __init__(self, message, defaults, backend):
        self.message = message
        self.defaults = defaults
        self.backend = backend
        self.esp_name = backend.esp_name

        self.init_payload()

        # we should consider hoisting the first text/html out of alternatives into set_html_body
        message_attrs = self.base_message_attrs + self.anymail_message_attrs + self.esp_message_attrs
        for attr, combiner, converter in message_attrs:
            value = getattr(message, attr, UNSET)
            if combiner is not None:
                default_value = self.defaults.get(attr, UNSET)
                value = combiner(default_value, value)
            if value is not UNSET:
                if converter is not None:
                    if not callable(converter):
                        converter = getattr(self, converter)
                    value = converter(value)
                if attr == 'body':
                    setter = self.set_html_body if message.content_subtype == 'html' else self.set_text_body
                else:
                    # AttributeError here? Your Payload subclass is missing a set_<attr> implementation
                    setter = getattr(self, 'set_%s' % attr)
                setter(value)

    def unsupported_feature(self, feature):
        if self.backend.unsupported_feature_errors:
            raise AnymailUnsupportedFeature("%s does not support %s" % (self.esp_name, feature),
                                            email_message=self.message, payload=self, backend=self.backend)

    #
    # Attribute converters
    #

    def parsed_email(self, address):
        return ParsedEmail(address, self.message.encoding)

    def parsed_emails(self, addresses):
        encoding = self.message.encoding
        return [ParsedEmail(address, encoding) for address in addresses]

    def prepped_attachments(self, attachments):
        str_encoding = self.message.encoding or settings.DEFAULT_CHARSET
        return [Attachment(attachment, str_encoding) for attachment in attachments]

    #
    # Abstract implementation
    #

    def init_payload(self):
        raise NotImplementedError("%s.%s must implement init_payload" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_from_email(self, email):
        raise NotImplementedError("%s.%s must implement set_from_email" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_to(self, emails):
        return self.set_recipients('to', emails)

    def set_cc(self, emails):
        return self.set_recipients('cc', emails)

    def set_bcc(self, emails):
        return self.set_recipients('bcc', emails)

    def set_recipients(self, recipient_type, emails):
        for email in emails:
            self.add_recipient(recipient_type, email)

    def add_recipient(self, recipient_type, email):
        raise NotImplementedError("%s.%s must implement add_recipient, set_recipients, or set_{to,cc,bcc}" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_subject(self, subject):
        raise NotImplementedError("%s.%s must implement set_subject" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_reply_to(self, emails):
        self.unsupported_feature('reply_to')

    def set_extra_headers(self, headers):
        self.unsupported_feature('extra_headers')

    def set_text_body(self, body):
        raise NotImplementedError("%s.%s must implement set_text_body" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_html_body(self, body):
        raise NotImplementedError("%s.%s must implement set_html_body" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_alternatives(self, alternatives):
        for content, mimetype in alternatives:
            self.add_alternative(content, mimetype)

    def add_alternative(self, content, mimetype):
        raise NotImplementedError("%s.%s must implement add_alternative or set_alternatives" %
                                  (self.__class__.__module__, self.__class__.__name__))

    def set_attachments(self, attachments):
        for attachment in attachments:
            self.add_attachment(attachment)

    def add_attachment(self, attachment):
        raise NotImplementedError("%s.%s must implement add_attachment or set_attachments" %
                                  (self.__class__.__module__, self.__class__.__name__))

    # Anymail-specific payload construction
    def set_metadata(self, metadata):
        self.unsupported_feature("metadata")

    def set_send_at(self, send_at):
        self.unsupported_feature("send_at")

    def set_tags(self, tags):
        self.unsupported_feature("tags")

    def set_track_clicks(self, track_clicks):
        self.unsupported_feature("track_clicks")

    def set_track_opens(self, track_opens):
        self.unsupported_feature("track_opens")

    # ESP-specific payload construction
    def set_esp_extra(self, extra):
        self.unsupported_feature("esp_extra")


class RequestsPayload(BasePayload):
    """Abstract Payload for AnymailRequestsBackend"""

    def __init__(self, message, defaults, backend,
                 method="POST", params=None, data=None,
                 headers=None, files=None, auth=None):
        self.method = method
        self.params = params
        self.data = data
        self.headers = headers
        self.files = files
        self.auth = auth
        super(RequestsPayload, self).__init__(message, defaults, backend)

    def get_request_params(self, api_url):
        """Returns a dict of requests.request params that will send payload to the ESP.

        :param api_url: the base api_url for the backend
        :return: dict
        """
        api_endpoint = self.get_api_endpoint()
        if api_endpoint is not None:
            url = urljoin(api_url, api_endpoint)
        else:
            url = api_url

        return dict(
            method=self.method,
            url=url,
            params=self.params,
            data=self.serialize_data(),
            headers=self.headers,
            files=self.files,
            auth=self.auth,
            # json= is not here, because we prefer to do our own serialization
            #       to provide extra context in error messages
        )

    def get_api_endpoint(self):
        """Returns a str that should be joined to the backend's api_url for sending this payload."""
        return None

    def serialize_data(self):
        """Performs any necessary serialization on self.data, and returns the result."""
        return self.data

    def serialize_json(self, data):
        """Returns data serialized to json, raising appropriate errors.

        Useful for implementing serialize_data in a subclass,
        """
        try:
            return json.dumps(data)
        except TypeError as err:
            # Add some context to the "not JSON serializable" message
            raise AnymailSerializationError(orig_err=err, email_message=self.message,
                                            backend=self.backend, payload=self)