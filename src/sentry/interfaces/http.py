"""
sentry.interfaces.http
~~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2014 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

from __future__ import absolute_import

__all__ = ('Http', )

import re
import six

from django.conf import settings
from django.utils.translation import ugettext as _
from django.utils.http import urlencode
from six.moves.urllib.parse import parse_qsl, urlsplit, urlunsplit

from sentry.interfaces.base import Interface, InterfaceValidationError, prune_empty_keys
from sentry.interfaces.schemas import validate_and_default_interface
from sentry.utils import json
from sentry.utils.strings import to_unicode
from sentry.utils.safe import trim, trim_dict, trim_pairs, get_path
from sentry.utils.http import heuristic_decode
from sentry.utils.validators import validate_ip
from sentry.web.helpers import render_to_string

# Instead of relying on a list of hardcoded methods, just loosly match
# against a pattern.
http_method_re = re.compile(r'^[A-Z\-_]{3,32}$')


def format_headers(value):
    if not value:
        return ()

    if isinstance(value, dict):
        value = value.items()

    result = []
    cookie_header = None
    for k, v in value:
        # If a header value is a list of header,
        # we want to normalize this into a comma separated list
        # This is how most other libraries handle this.
        # See: urllib3._collections:HTTPHeaderDict.itermerged
        if isinstance(v, list):
            v = ', '.join(v)

        if k.lower() == 'cookie':
            cookie_header = v
        else:
            if not isinstance(v, six.string_types):
                v = six.text_type(v)
            result.append((k.title(), v))
    return result, cookie_header


def format_cookies(value):
    if not value:
        return ()

    if isinstance(value, six.string_types):
        value = parse_qsl(value, keep_blank_values=True)

    if isinstance(value, dict):
        value = value.items()

    return [list(map(fix_broken_encoding, (k.strip(), v))) for k, v in value]


def fix_broken_encoding(value):
    """
    Strips broken characters that can't be represented at all
    in utf8. This prevents our parsers from breaking elsewhere.
    """
    if isinstance(value, six.text_type):
        value = value.encode('utf8', errors='replace')
    if isinstance(value, six.binary_type):
        value = value.decode('utf8', errors='replace')
    return value


def jsonify(value):
    return to_unicode(value) if isinstance(value, six.string_types) else json.dumps(value)


class Http(Interface):
    """
    The Request information is stored in the Http interface. Two arguments
    are required: ``url`` and ``method``.

    The ``env`` variable is a compounded dictionary of HTTP headers as well
    as environment information passed from the webserver. Sentry will explicitly
    look for ``REMOTE_ADDR`` in ``env`` for things which require an IP address.

    The ``data`` variable should only contain the request body (not the query
    string). It can either be a dictionary (for standard HTTP requests) or a
    raw request body.

    >>>  {
    >>>     "url": "http://absolute.uri/foo",
    >>>     "method": "POST",
    >>>     "data": "foo=bar",
    >>>     "query_string": "hello=world",
    >>>     "cookies": "foo=bar",
    >>>     "headers": [
    >>>         ["Content-Type", "text/html"]
    >>>     ],
    >>>     "env": {
    >>>         "REMOTE_ADDR": "192.168.0.1"
    >>>     }
    >>>  }

    .. note:: This interface can be passed as the 'request' key in addition
              to the full interface path.
    """
    display_score = 1000
    score = 800
    path = 'request'

    FORM_TYPE = 'application/x-www-form-urlencoded'

    @classmethod
    def to_python(cls, data):
        is_valid, errors = validate_and_default_interface(data, cls.path)
        if not is_valid:
            raise InterfaceValidationError("Invalid interface data")

        kwargs = {}

        if data.get('method'):
            method = data['method'].upper()
            # Optimize for the common path here, where it's a GET/POST, falling
            # back to a regular expresion test
            if method not in ('GET', 'POST') and not http_method_re.match(method):
                raise InterfaceValidationError("Invalid value for 'method'")
            kwargs['method'] = method
        else:
            kwargs['method'] = None

        if data.get('url', None):
            url = to_unicode(data['url'])
            # The JavaScript SDK used to send an ellipsis character for
            # truncated URLs. Canonical URLs do not contain UTF-8 characters in
            # either the path, query string or fragment, so we replace it with
            # three dots (which is the behavior of other SDKs). This effectively
            # makes the string two characters longer, but it will be trimmed
            # again down below.
            if url.endswith(u"\u2026"):
                url = url[:-1] + "..."
            scheme, netloc, path, query_bit, fragment_bit = urlsplit(url)
        else:
            scheme = netloc = path = query_bit = fragment_bit = None

        query_string = data.get('query_string') or query_bit
        if query_string:
            if isinstance(query_string, six.string_types):
                if query_string[0] == '?':
                    query_string = query_string[1:]
                if query_string.endswith(u"\u2026"):
                    query_string = query_string[:-1] + "..."
                query_string = [
                    (to_unicode(k), jsonify(v))
                    for k, v in parse_qsl(query_string, keep_blank_values=True)
                ]
            elif isinstance(query_string, dict):
                query_string = [(to_unicode(k), jsonify(v)) for k, v in six.iteritems(query_string)]
            elif isinstance(query_string, list):
                query_string = [
                    tuple(tup) for tup in query_string
                    if isinstance(tup, (tuple, list)) and len(tup) == 2
                ]
            else:
                query_string = []
            kwargs['query_string'] = trim(query_string, 4096)
        else:
            kwargs['query_string'] = []

        fragment = data.get('fragment') or fragment_bit

        cookies = data.get('cookies')
        # if cookies were [also] included in headers we
        # strip them out
        if data.get("headers"):
            headers, cookie_header = format_headers(get_path(data, "headers", filter=True))
            if not cookies and cookie_header:
                cookies = cookie_header
        else:
            headers = ()

        # We prefer the body to be a string, since we can then attempt to parse it
        # as JSON OR decode it as a URL encoded query string, without relying on
        # the correct content type header being passed.
        body = data.get('data')

        content_type = next((v for k, v in headers if k == 'Content-Type'), None)

        # Remove content type parameters
        if content_type is not None:
            content_type = content_type.partition(';')[0].rstrip()

        # We process request data once during ingestion and again when
        # requesting the http interface over the API. Avoid overwriting
        # decoding the body again.
        inferred_content_type = data.get('inferred_content_type', content_type)

        if 'inferred_content_type' not in data and not isinstance(body, dict):
            body, inferred_content_type = heuristic_decode(body, content_type)

        if body:
            body = trim(body, settings.SENTRY_MAX_HTTP_BODY_SIZE)

        env = data.get('env', {})
        # TODO (alex) This could also be accomplished with schema (with formats)
        if 'REMOTE_ADDR' in env:
            try:
                validate_ip(env['REMOTE_ADDR'], required=False)
            except ValueError:
                del env['REMOTE_ADDR']

        kwargs['inferred_content_type'] = inferred_content_type
        kwargs['cookies'] = trim_pairs(format_cookies(cookies))
        kwargs['env'] = trim_dict(env)
        kwargs['headers'] = trim_pairs(headers)
        kwargs['data'] = fix_broken_encoding(body)
        kwargs['url'] = urlunsplit((scheme, netloc, path, '', ''))
        kwargs['fragment'] = trim(fragment, 1024)

        return cls(**kwargs)

    def to_json(self):
        return prune_empty_keys({
            'method': self.method,
            'url': self.url,
            'query_string': self.query_string or None,
            'fragment': self.fragment or None,
            'cookies': self.cookies or None,
            'headers': self.headers or None,
            'data': self.data,
            'env': self.env or None,
            'inferred_content_type': self.inferred_content_type,
        })

    @property
    def full_url(self):
        url = self.url
        if url:
            if self.query_string:
                url = url + '?' + urlencode(self.query_string)
            if self.fragment:
                url = url + '#' + self.fragment
        return url

    def to_email_html(self, event, **kwargs):
        return render_to_string(
            'sentry/partial/interfaces/http_email.html', {
                'event': event,
                'url': self.full_url,
                'short_url': self.url,
                'method': self.method,
                'query_string': urlencode(self.query_string),
                'fragment': self.fragment,
            }
        )

    def get_title(self):
        return _('Request')

    def get_api_context(self, is_public=False):
        if is_public:
            return {}

        cookies = self.cookies or ()
        if isinstance(cookies, dict):
            cookies = sorted(self.cookies.items())

        headers = self.headers or ()
        if isinstance(headers, dict):
            headers = sorted(self.headers.items())

        data = {
            'method': self.method,
            'url': self.url,
            'query': self.query_string,
            'fragment': self.fragment,
            'data': self.data,
            'headers': headers,
            'cookies': cookies,
            'env': self.env or None,
            'inferredContentType': self.inferred_content_type,
        }
        return data

    def get_api_meta(self, meta, is_public=False):
        if is_public:
            return None

        headers = meta.get('headers')
        if headers:
            headers_meta = headers.pop('', None)
            headers = {six.text_type(i): {'1': h[1]} for i, h in enumerate(sorted(headers.items()))}
            if headers_meta:
                headers[''] = headers_meta

        cookies = meta.get('cookies')
        if cookies:
            cookies_meta = cookies.pop('', None)
            cookies = {six.text_type(i): {'1': h[1]} for i, h in enumerate(sorted(cookies.items()))}
            if cookies_meta:
                cookies[''] = cookies_meta

        return {
            '': meta.get(''),
            'method': meta.get('method'),
            'url': meta.get('url'),
            'query': meta.get('query_string'),
            'data': meta.get('data'),
            'headers': headers,
            'cookies': cookies,
            'env': meta.get('env'),
        }
