import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect

import ModSecurity

SETTINGS_NAMES = {
    'rule_files': 'MODSECURITY_RULE_FILES',
    'rule_lines': 'MODSECURITY_RULES',
}


class PyModSecurityMiddleware(object):
    def __init__(self, get_response):
        '''
        PyModSecurityMiddleware
        This integrates the python bindings to the lib modsecurity to the
        django ecosystem

        :param callable get_response
        '''
        self.logger = logging.getLogger(__name__)

        self.get_response = get_response

        self.modsecurity = ModSecurity.ModSecurity()
        self.modsecurity.setServerLogCb(self.modsecurity_log_callback)
        self.rules = ModSecurity.Rules()

        self.rule_files = getattr(settings, SETTINGS_NAMES['rule_files'], None)
        if isinstance(self.rule_files, str):
            self.rule_files = [self.rule_files]

        self.rule_lines = getattr(settings, SETTINGS_NAMES['rule_lines'], None)
        if isinstance(self.rule_lines, list):
            self.rule_lines = '\n'.join(self.rule_lines)

        self._rules_count = 0
        if self.rule_files is not None:
            self.load_rule_files(self.rule_files)

        if self.rule_lines is not None:
            self.load_rules(self.rule_lines)

    def modsecurity_log_callback(self, data, msg):
        self.logger.info(msg)

    @property
    def rules_count(self):
        return self._rules_count

    def load_rule_files(self, rule_files):
        '''
        Process a list of files (can be a list of globs) and loads into modsecurity
        :param list(str) rule_files
        :rtype: int
        :return the total rules that were loaded
        '''
        before_count = self.rules_count
        import glob
        for pattern in rule_files:
            for rule_file in glob.glob(pattern, recursive=True):
                rules_count = self.rules.loadFromUri(rule_file)
                if rules_count < 0:
                    msg = f'[ModSecurity] Error trying to load rule file {rule_file}. {self.rules.getParserError()}'

                    self.logger.warning(msg)
                else:
                    self._rules_count += rules_count

        return self.rules_count - before_count

    def load_rules(self, rules):
        '''
        Process rules
        :param str: rules
        :rtype: int
        :return the total rules that were loaded
        '''
        if rules is None or len(rules) <= 0:
            return 0

        rules_count = self.rules.load(rules)
        if rules_count < 0:
            msg = '[ModSecurity] Error trying to load rules: %s' % self.rules.getParserError(
            )
            self.logger.warning(msg)
        else:
            self._rules_count += rules_count

        return rules_count

    def __call__(self, request):
        transaction = ModSecurity.Transaction(self.modsecurity, self.rules)
        response = self.process_request(request, transaction)

        # We got an intervention response when processing the request
        # Do not proceed!
        if response is not None:
            return response

        response = self.get_response(request)
        response = self.process_response(request, response, transaction)
        return response

    def process_request(self, request, transaction):
        '''
        Process a request and checks with modsecurity if it's safe or if it should
        make an intervention
        '''
        meta = request.META
        transaction.processConnection(meta['REMOTE_ADDR'],
                                      int(request.get_port()),
                                      meta['SERVER_NAME'],
                                      int(meta['SERVER_PORT']))

        response = self.process_intervention(transaction)
        if response is not None:
            return response

        transaction.processURI(request.path, request.method, '1.1')
        response = self.process_intervention(transaction)
        if response is not None:
            return response

        for key, value in self._iter_headers(request):
            transaction.addRequestHeader(key, value)

        transaction.processRequestHeaders()
        response = self.process_intervention(transaction)
        if response is not None:
            return response

        transaction.appendRequestBody(request.body)
        transaction.processRequestBody()
        response = self.process_intervention(transaction)
        return response if response is not None else None

    def _iter_headers(self, request):
        for key, value in request.META.items():
            if key.startswith('HTTP_'):
                yield key[5:], value

    def process_response(self, request, original_response, transaction):
        '''
        Process a response and checks with modsecurity if it's safe or if it should
        make an intervention
        '''
        for field, value in original_response.items():
            transaction.addResponseHeader(field, value)

        transaction.processResponseHeaders(original_response.status_code,
                                           'HTTP/1.1')

        response = self.process_intervention(transaction)
        if response is not None:
            return response

        transaction.appendResponseBody(original_response.getvalue())
        transaction.processResponseBody()

        response = self.process_intervention(transaction)
        return response if response is not None else original_response

    def process_intervention(self, transaction):
        '''
        Check if there's interventions
        :return the apropriate response, if any:
        :rtype HttpResponse:
        '''
        intervention = ModSecurity.ModSecurityIntervention()

        if intervention is None:
            return None

        if not transaction.intervention(intervention):
            return None
        if intervention.log is not None:
            self.logger.info(intervention.log)

        if not intervention.disruptive:
            return None

        return (
            HttpResponseRedirect(intervention.url)
            if intervention.url is not None
            else HttpResponse(status=intervention.status)
        )
