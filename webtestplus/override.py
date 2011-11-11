# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 1.1/GPL 2.0/LGPL 2.1
#
# The contents of this file are subject to the Mozilla Public License Version
# 1.1 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
# http://www.mozilla.org/MPL/
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License
# for the specific language governing rights and limitations under the
# License.
#
# The Original Code is Sync Server
#
# The Initial Developer of the Original Code is the Mozilla Foundation.
# Portions created by the Initial Developer are Copyright (C) 2010
# the Initial Developer. All Rights Reserved.
#
# Contributor(s):
#   Tarek Ziade (tarek@mozilla.com)
#
# Alternatively, the contents of this file may be used under the terms of
# either the GNU General Public License Version 2 or later (the "GPL"), or
# the GNU Lesser General Public License Version 2.1 or later (the "LGPL"),
# in which case the provisions of the GPL or the LGPL are applicable instead
# of those above. If you wish to allow use of your version of this file only
# under the terms of either the GPL or the LGPL, and not to allow others to
# use your version of this file under the terms of the MPL, indicate your
# decision by deleting the provisions above and replace them with the notice
# and other provisions required by the GPL or the LGPL. If you do not delete
# the provisions above, a recipient may use your version of this file under
# the terms of any one of the MPL, the GPL or the LGPL.
#
# ***** END LICENSE BLOCK *****
""" Test helpers
"""
from collections import defaultdict
import json
import time

from webob.dec import wsgify
from webob import exc

__all__ = ['ClientTesterMiddleware']


DISABLED = 'disabled'
RECORD = 'recording'
REPLAY = 'playing'



def _int2status(status):
    if status == 200:
        return '200 OK'
    if status == 400:
        return '400 Bad Request'
    if status == '401':
        return '400 Unauthorized'

    return '%d Explanation' % status


class ClientTesterMiddleware(object):
    """Middleware that let a client drive failures for testing purposes.
    """
    def __init__(self, app, mock_path='/__testing__',
                 filter_path='/__filter__',
                 rec_path='/__record__'):
        self.app = app
        self.mock_path = mock_path
        self.filter_path = filter_path
        self.rec_path = rec_path
        self.replays = defaultdict(list)
        self.filters = defaultdict(dict)
        self.is_recording = defaultdict(lambda: DISABLED)

    def _get_client_ip(self, environ):
        if 'HTTP_X_FORWARDED_FOR' in environ:
            return environ['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()

        if 'REMOTE_ADDR' in environ:
            return environ['REMOTE_ADDR']

        return None

    def _resp(self, req, status='200 OK', body='', headers=None):
        resp = req.response
        resp.status = status
        resp.body = body

        import pdb; pdb.set_trace()
        if headers is not None:
            headers = [(key, value.encode('utf8'))
                        for key, value in headers.items()]
            resp.headers = headers

        return resp


    def _apply_filters(self, resp, filters):
        status = resp.status
        intst = int(status.split()[0])
        if intst in filters:
            time.sleep(filters[intst])
        elif '*' in filters:
            time.sleep(filters['*'])

        # XXX maybe we will have filters that change the resp
        return resp

    @wsgify
    def __call__(self, request):
        environ = request.environ
        path = request.path_info

        environ['_ip'] = ip = self._get_client_ip(environ)
        environ['_replays'] = replays = self.replays[ip]
        environ['_filters'] = filters = self.filters[ip]
        environ['_is_recording'] = rec = self.is_recording[ip]

        # routing
        if path.startswith(self.mock_path):
            return self._mock(request)
        elif path.startswith(self.filter_path):
            return self._filter(request)
        elif path.startswith(self.rec_path):
            return self._rec_state(request)

        # classical call, do we have something to replay ?
        if len(replays) > 0:
            # yes
            replay = replays.pop()
            status = _int2status(replay['status'])
            body = replay.get('body', u'').encode('utf8')
            headers = replay.get('headers')
            delay = replay.get('delay', 0)

            # repeat it, always
            if replay.get('repeat') == -1:
                replays.insert(0, replay)

            # build the response
            resp = request.response
            resp.status = status
            resp.body = body
            resp.headers = headers

            # apply filters
            resp = self._apply_filters(resp, filters)

            # extra delay
            time.sleep(delay)

            return resp
        else:
            # no, regular app
            # do we record or play or just call the app ?
            if rec == DISABLED:
                resp = request.get_response(self.app)
            elif rec == REPLAY:
                resp = self._replay(request)
            elif rec == RECORD:
                resp = self._record(request)

            return self._apply_filters(resp, filters)

    def _checkmeth(self, method, allowed=None):
        if allowed is None:
            allowed = ('POST', 'DELETE')

        if method not in allowed:
            raise exc.HTTPMethodNotAllowed(
                              allow=','.join(allowed))

    def _rec_state(self, request):
        environ = request.environ

        ip = environ['_ip']
        # what's the method ?
        method = environ['REQUEST_METHOD']
        allowed = ('POST', 'GET')

        self._checkmeth(method, allowed)

        if method == 'POST':
            # define the toggle
            try:
                st = json.loads(environ['wsgi.input'].read())
            except ValueError:
                return self._resp(start_response, '400 Bad Request')
            self.is_recording[ip] = st
            return self._resp(request)

        status = json.dumps(self.is_recording[ip])
        return self._resp(request, body=status)

    def _mock(self, request):
        # what's the method ?
        method = request.method
        self._checkmeth(method)
        replays = request.environ['_replays']

        if method == 'DELETE':
            # wipe out
            replays[:] = []
            return self._resp(request)

        # that's something to add to the pile
        try:
            resp = json.loads(request.body)
        except ValueError:
            raise exc.HTTPBadRequest()

        repeat = resp.get('repeat', 1)
        if repeat == -1:
            # will repeat indefinitely
            replays.insert(0, resp)
        else:
            for i in range(repeat):
                replays.insert(0, resp)

        return self._resp(request)

    def _filter(self, request):
        # what's the method ?
        method = request.method
        self._checkmeth(method)
        filters = request.environ['_filters']

        if method == 'DELETE':
            # wipe out
            filters.clear()
            return self._resp(request)

        # that's something to set
        try:
            new = json.loads(request.body)
        except ValueError:
            raise exc.HTTPBadRequest()

        filters.clear()

        for status, delay in new.items():
            if status != '*':
                status = int(status)
            filters[status] = delay

        return self._resp(request)

    def _replay(environ, start_response):
        raise NotImplementedError()

    def _record(environ, start_response):
        raise NotImplementedError()
