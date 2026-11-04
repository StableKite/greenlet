"""Run the complete upstream unittest discovery with original files unchanged."""
from pathlib import Path
import faulthandler
import hashlib
import json
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
out.mkdir(exist_ok=False)
assert 'PYTHON_GIL' not in os.environ
assert not sys._is_gil_enabled() and sys.flags.gil is None
faulthandler.dump_traceback_later(150, exit=True)
if '--source-fixture' in sys.argv:
    fixture = Path(sys.argv[sys.argv.index('--source-fixture')+1])
    sys.path.insert(0, str(fixture/'src'))
import greenlet
import greenlet.tests
from greenlet.tests import leakcheck
assert not leakcheck.SKIP_LEAKCHECKS and not leakcheck.SKIP_FAILING_LEAKCHECKS
assert not leakcheck.ONLY_FAILING_LEAKCHECKS
tests = Path(greenlet.tests.__file__).parent
before = {str(path.relative_to(tests)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in tests.rglob('*') if path.is_file() and '__pycache__' not in path.parts}

class Result(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.started = time.monotonic()
        self.observations.append({'id': test.id(), 'status': 'passed'})

    def stopTest(self, test):
        self.observations[-1]['seconds'] = time.monotonic()-self.started
        super().stopTest(test)

    def addFailure(self, test, error):
        self.observations[-1].update(status='failure', detail=self._exc_info_to_string(error, test))
        super().addFailure(test, error)

    def addError(self, test, error):
        self.observations[-1].update(status='error', detail=self._exc_info_to_string(error, test))
        super().addError(test, error)

    def addSkip(self, test, reason):
        self.observations[-1].update(status='skipped', detail=reason)
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, error):
        self.observations[-1].update(status='expected_failure', detail=self._exc_info_to_string(error, test))
        super().addExpectedFailure(test, error)

    def addUnexpectedSuccess(self, test):
        self.observations[-1].update(status='unexpected_success')
        super().addUnexpectedSuccess(test)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = []

suite = (unittest.defaultTestLoader.loadTestsFromNames([
    'greenlet.tests.test_generator_nested.NestedGeneratorTests.test_permutations',
    'greenlet.tests.test_greenlet.TestGreenlet.test_recursive_startup'])
    if 'two-known-leak-cases' in sys.argv[2:] else unittest.defaultTestLoader.discover('greenlet.tests'))
result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
after = {str(path.relative_to(tests)): hashlib.sha256(path.read_bytes()).hexdigest()
         for path in tests.rglob('*') if path.is_file() and '__pycache__' not in path.parts}
assert before == after
assert not sys._is_gil_enabled() and sys.flags.gil is None
root = ET.Element('testsuite', name='original-greenlet-unittest', tests=str(result.testsRun),
                  failures=str(len(result.failures)), errors=str(len(result.errors)), skipped=str(len(result.skipped)))
for item in result.observations:
    case = ET.SubElement(root, 'testcase', name=item['id'], time=str(item.get('seconds', 0)))
    if item['status'] in ('failure', 'error'):
        ET.SubElement(case, item['status']).text = item.get('detail', '')
    elif item['status'] in ('skipped', 'expected_failure'):
        ET.SubElement(case, 'skipped', message=item.get('detail', ''))
ET.ElementTree(root).write(out/'TESTS.xml', encoding='utf-8', xml_declaration=True)
(out/'RESULT.json').write_text(json.dumps({'status': 'PASS_ORIGINAL_MAIN_SUITE_NOT_SI' if result.wasSuccessful() else 'FAILED_ORIGINAL_MAIN_SUITE',
    'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
    'skips': [{'test': test.id(), 'reason': reason} for test, reason in result.skipped],
    'scope': 'two-known-leak-cases' if 'two-known-leak-cases' in sys.argv[2:] else 'complete-original-discovery',
    'observations': result.observations, 'original_files_sha256_before_after': before,
    'GIL_after': sys._is_gil_enabled(), 'gil_flag': sys.flags.gil,
    'version': greenlet.__version__, 'native_path': greenlet._greenlet.__file__,
    'subinterpreter_status': 'This main-suite packet does not qualify SI; genuine child interpreter evidence is recorded separately.'}, indent=2)+'\n')
faulthandler.cancel_dump_traceback_later()
raise SystemExit(0 if result.wasSuccessful() else 1)
