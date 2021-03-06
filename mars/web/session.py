# Copyright 1999-2018 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import time
import logging

import requests

from ..compat import six, TimeoutError  # pylint: disable=W0622
from ..serialize import dataserializer
from ..errors import ResponseMalformed, ExecutionInterrupted, ExecutionFailed, \
    ExecutionStateUnknown, ExecutionNotStopped
from ..utils import build_graph

logger = logging.getLogger(__name__)


class Session(object):
    def __init__(self, endpoint, req_session=None, args=None):
        self._endpoint = endpoint
        self._args = args
        # dict structure: {tensor_key -> graph_key, tensor_ids}
        # dict value is a tuple object which records graph key and tensor id
        self._executed_tensors = dict()

        if req_session:
            self._req_session = req_session
        else:
            from requests.adapters import HTTPAdapter

            self._req_session = requests.Session()
            self._req_session.mount('http://stackoverflow.com', HTTPAdapter(max_retries=5))

        self._main()

    @property
    def session_id(self):
        return self._session_id

    @property
    def endpoint(self):
        return self._endpoint

    @endpoint.setter
    def endpoint(self, url):
        self._endpoint = url

    def _main(self):
        resp = self._req_session.post(self._endpoint + '/api/session', self._args)
        if resp.status_code >= 400:
            raise SystemError('Failed to create mars session.')
        content = json.loads(resp.text)
        self._session_id = content['session_id']

    def _get_tensor_graph_key(self, tensor_key):
        return self._executed_tensors[tensor_key][0]

    def _set_tensor_graph_key(self, tensor, graph_key):
        tensor_key = tensor.key
        tensor_id = tensor.id
        if tensor_key in self._executed_tensors:
            self._executed_tensors[tensor_key][1].add(tensor_id)
        else:
            self._executed_tensors[tensor_key] = graph_key, {tensor_id}

    def _check_response_finished(self, graph_url):
        try:
            resp = self._req_session.get(graph_url)
        except requests.ConnectionError as ex:
            err_msg = str(ex)
            if 'ConnectionResetError' in err_msg or 'Connection refused' in err_msg:
                return False
            raise

        if resp.status_code == 504:
            logging.debug('Gateway Time-out, try again')
            return False
        if resp.status_code >= 400:
            raise SystemError('Failed to obtain execution status. Code: %d, Reason: %s, Content:\n%s' %
                              (resp.status_code, resp.reason, resp.text))
        try:
            resp_json = json.loads(resp.text)
        except ValueError:
            raise ResponseMalformed('Response malformed. Code: %d, Content:\n%s' %
                                    (resp.status_code, resp.text))
        if resp_json['state'] == 'success':
            return True
        elif resp_json['state'] in ('running', 'preparing'):
            return False
        elif resp_json['state'] in ('cancelled', 'cancelling'):
            raise ExecutionInterrupted
        elif resp_json['state'] == 'failed':
            # TODO add traceback
            if 'traceback' in resp_json:
                traceback = resp_json['traceback']
                traceback = ''.join(str(s) for s in traceback) \
                    if isinstance(traceback, list) else traceback
                raise ExecutionFailed(
                    'Graph execution failed.\nMessage: %s\nTraceback from server:\n%s' %
                    (resp_json['msg'], traceback))
            else:
                raise ExecutionFailed('Graph execution failed with unknown reason.')
        raise ExecutionStateUnknown(
            'Unknown graph execution state %s' % resp_json['state'])

    def run(self, *tensors, **kw):
        timeout = kw.pop('timeout', -1)
        compose = kw.pop('compose', True)
        fetch = kw.pop('fetch', True)
        if kw:
            raise TypeError('run got unexpected key arguments {0}'.format(', '.join(kw.keys())))

        # those executed tensors should fetch data directly, submit the others
        run_tensors = [t for t in tensors if t.key not in self._executed_tensors]

        graph = build_graph(run_tensors, executed_keys=list(self._executed_tensors.keys()))
        targets = [t.key for t in run_tensors]

        targets_join = ','.join(targets)
        session_url = self._endpoint + '/api/session/' + self._session_id
        graph_json = graph.to_json()

        resp_json = self._submit_graph(graph_json, targets_join, compose=compose)
        graph_key = resp_json['graph_key']
        graph_url = session_url + '/graph/' + graph_key

        for t in tensors:
            self._set_tensor_graph_key(t, graph_key)

        exec_start_time = time.time()
        while timeout <= 0 or time.time() - exec_start_time <= timeout:
            try:
                time.sleep(1)
                if self._check_response_finished(graph_url):
                    break
            except KeyboardInterrupt:
                resp = self._req_session.delete(graph_url)
                if resp.status_code >= 400:
                    raise ExecutionNotStopped(
                        'Failed to stop graph execution. Code: %d, Reason: %s, Content:\n%s' %
                        (resp.status_code, resp.reason, resp.text))

        if 0 < timeout < time.time() - exec_start_time:
            raise TimeoutError
        if not fetch:
            return
        else:
            return self.fetch(*tensors)

    def fetch(self, *tensors, **kw):
        timeout = kw.pop('timeout', None)
        if kw:
            raise TypeError('fetch got unexpected key arguments {0}'.format(', '.join(kw.keys())))

        results = list()
        for tensor in tensors:
            key = tensor.key

            if key not in self._executed_tensors:
                raise ValueError('Cannot fetch the unexecuted tensor')

            session_url = self._endpoint + '/api/session/' + self._session_id
            data_url = session_url + '/graph/%s/data/%s' % (self._get_tensor_graph_key(key), key)
            resp = self._req_session.get(data_url, timeout=timeout)
            if resp.status_code >= 400:
                raise ValueError('Failed to fetch data from server. Code: %d, Reason: %s, Content:\n%s' %
                                 (resp.status_code, resp.reason, resp.text))
            results.append(dataserializer.loads(resp.content))
        return results

    def _update_tensor_shape(self, tensor):
        tensor_key = tensor.key
        session_url = self._endpoint + '/api/session/' + self._session_id
        url = session_url + '/graph/%s/data/%s?type=nsplits' % (self._get_tensor_graph_key(tensor_key), tensor_key)
        resp = self._req_session.get(url)
        new_nsplits = json.loads(resp.text)
        tensor._update_shape(tuple(sum(nsplit) for nsplit in new_nsplits))
        tensor.nsplits = new_nsplits

    def decref(self, *keys):
        session_url = self._endpoint + '/api/session/' + self._session_id
        for tensor_key, tensor_id in keys:
            if tensor_key not in self._executed_tensors:
                continue
            graph_key, ids = self._executed_tensors[tensor_key]

            if tensor_id in ids:
                ids.remove(tensor_id)
                # for those same key tensors, do decref only when all those tensors are garbage collected
                if len(ids) != 0:
                    continue
                data_url = session_url + '/graph/%s/data/%s' % (graph_key, tensor_key)
                self._req_session.delete(data_url)
                del self._executed_tensors[tensor_key]

    def stop(self, graph_key):
        session_url = self._endpoint + '/api/session/' + self._session_id
        graph_url = session_url + '/graph/' + graph_key
        resp = self._req_session.delete(graph_url)
        if resp.status_code >= 400:
            raise SystemError('Failed to stop graph execution. Code: %d, Reason: %s, Content:\n%s' %
                              (resp.status_code, resp.reason, resp.text))

    def _submit_graph(self, graph_json, targets, compose=True):
        session_url = self._endpoint + '/api/session/' + self._session_id
        resp = self._req_session.post(session_url + '/graph', dict(
            graph=json.dumps(graph_json),
            target=targets,
            compose=compose
        ))
        if resp.status_code >= 400:
            resp_json = json.loads(resp.text)
            exc_info = base64.b64decode(resp_json['exc_info'])
            six.reraise(*exc_info)
        resp_json = json.loads(resp.text)
        return resp_json

    def get_graph_states(self):
        session_url = self._endpoint + '/api/session/' + self._session_id
        resp = self._req_session.get(session_url + '/graph')
        if resp.status_code >= 400:
            resp_json = json.loads(resp.text)
            exc_info = base64.b64decode(resp_json['exc_info'])
            six.reraise(*exc_info)
        resp_json = json.loads(resp.text)
        return resp_json

    def close(self):
        executed_keys = []
        for key, value in self._executed_tensors.items():
            for tid in value[1]:
                executed_keys.append((key, tid))
        self.decref(*executed_keys)
        resp = self._req_session.delete(self._endpoint + '/api/session/' + self._session_id)
        if resp.status_code >= 400:
            raise SystemError('Failed to close mars session.')

    def check_service_ready(self, timeout=1):
        try:
            resp = self._req_session.get(self._endpoint + '/api', timeout=timeout)
        except (requests.ConnectionError, requests.Timeout):
            return False
        if resp.status_code >= 400:
            return False
        return True

    def count_workers(self):
        resp = self._req_session.get(self._endpoint + '/api/worker', timeout=1)
        return json.loads(resp.text)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
