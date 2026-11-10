"""Run with PYTHONPATH=lib python -m unittest discover -s tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import ansible


LOCAL_CLI = '''
import hashlib
from unittest.mock import patch
from ansible.plugins.connection.local import Connection
from ansible.plugins.action.fetch import ActionModule as FetchAction
original = Connection.__init__
def initialize(self, *args, **kwargs):
    with patch.dict('os.environ', {'ANSIBLE_SUPER_MODE': '1'}):
        original(self, *args, **kwargs)
Connection.__init__ = initialize
# The fixture target is local; preserve fetch's actual controller-side file handling.
def remote_stat(self, source, **kwargs):
    return {'exists': True, 'isdir': False, 'checksum': hashlib.sha1(Path(source).read_bytes()).hexdigest()}
FetchAction._execute_remote_stat = remote_stat
from ansible.cli.playbook import main
main(['ansible-playbook', '-i', 'fixture_host,', '-c', 'local', str(project / 'main.yml')])
'''


class PluginSourceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.marker = self.root / 'imported'

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def plugin(self, name, body):
        return self.write(name, 'from pathlib import Path\nPath(%r).write_text("imported")\n%s' % (str(self.marker), body))

    def run_python(self, source, trusted=False):
        env = dict(os.environ, PYTHONPATH=str(Path(ansible.__file__).resolve().parent.parent),
                   PYTHONDONTWRITEBYTECODE='1', ANSIBLE_SUPER_MODE='1' if trusted else '',
                   ANSIBLE_LOCAL_TEMP=str(self.root / 'local-tmp'), ANSIBLE_REMOTE_TEMP=str(self.root / 'remote-tmp'),
                   ANSIBLE_NOCOLOR='1')
        setup = 'import os, sys\nfrom pathlib import Path\nroot=Path(%r)\nproject=Path(%r)\n' % (str(self.root), str(self.project))
        return subprocess.run([sys.executable, '-c', setup + source], cwd=self.project, env=env,
                              capture_output=True, text=True, timeout=40)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rejects_direct_and_cached_action_plugin_imports(self):
        self.plugin('project/action_plugins/origin_probe.py',
                    'from ansible.plugins.action import ActionBase\nclass ActionModule(ActionBase): pass\n')
        result = self.run_python('''
from unittest.mock import patch
from ansible.errors import AnsibleError
from ansible.plugins.loader import action_loader, init_plugin_loader
init_plugin_loader()
action_loader.add_directory(str(project / 'action_plugins'))
for operation in (lambda: action_loader.get('origin_probe', class_only=True),
                  lambda: action_loader._load_module_source('origin_probe', str(project / 'action_plugins/origin_probe.py'))):
    try:
        operation()
    except AnsibleError as e:
        assert 'User-supplied Ansible plugin code is disabled' in str(e)
    else:
        raise AssertionError('untrusted plugin accepted')
assert not (root / 'imported').exists()
with patch.dict(os.environ, {'ANSIBLE_SUPER_MODE': '1'}):
    assert action_loader.get('origin_probe', class_only=True)
assert (root / 'imported').exists()
try:
    action_loader.get('origin_probe', class_only=True)
except AnsibleError:
    pass
else:
    raise AssertionError('cached untrusted plugin accepted')
''')
        self.assert_success(result)

    def test_enumeration_does_not_import_user_callbacks_filters_or_tests(self):
        for subdir, classname, method in (
            ('callback_plugins', 'CallbackModule', ''),
            ('filter_plugins', 'FilterModule', '    def filters(self): return {"origin_probe": lambda x: x}\n'),
            ('test_plugins', 'TestModule', '    def tests(self): return {"origin_probe": lambda x: True}\n'),
        ):
            self.plugin('project/%s/origin_probe.py' % subdir, 'class %s:\n%s' % (classname, method or '    pass\n'))
        result = self.run_python('''
from ansible.plugins.loader import callback_loader, filter_loader, test_loader, init_plugin_loader
init_plugin_loader()
for loader in (callback_loader, filter_loader, test_loader):
    loader.add_directory(str(project / loader.subdir))
    list(loader.all(class_only=True))
    assert not (root / 'imported').exists()
assert filter_loader.get('ansible.builtin.to_json')
''')
        self.assert_success(result)

    def test_module_and_module_utils_paths_are_restricted(self):
        self.plugin('project/library/origin_probe.py', '')
        self.plugin('project/module_utils/origin_probe.py', '')
        result = self.run_python('''
from ansible.errors import AnsibleError
from ansible.plugins.loader import module_loader, module_utils_loader, init_plugin_loader
init_plugin_loader()
module_loader.add_directory(str(project / 'library'))
module_utils_loader.add_directory(str(project / 'module_utils'))
try:
    module_loader.find_plugin('origin_probe', mod_type='.py')
except AnsibleError:
    pass
else:
    raise AssertionError('user module accepted')
assert str(project / 'module_utils') not in module_utils_loader._get_paths(subdirs=False)
assert module_loader.find_plugin('ansible.builtin.copy', mod_type='.py')
''')
        self.assert_success(result)
        self.assertFalse(self.marker.exists())

    def test_cached_vars_and_filter_plugins_are_revalidated(self):
        self.plugin('project/vars_plugins/origin_probe.py',
                    'from ansible.plugins.vars import BaseVarsPlugin\nclass VarsModule(BaseVarsPlugin): pass\n')
        self.plugin('project/filter_plugins/origin_probe.py',
                    'class FilterModule:\n    def filters(self): return {"origin_probe": lambda x: x}\n')
        result = self.run_python('''
from unittest.mock import patch
from ansible.errors import AnsibleError
from ansible.plugins.loader import vars_loader, filter_loader, init_plugin_loader
init_plugin_loader()
with patch.dict(os.environ, {'ANSIBLE_SUPER_MODE': '1'}):
    for loader in (vars_loader, filter_loader):
        loader.add_directory(str(project / loader.subdir))
        assert loader.get('origin_probe')
for loader in (vars_loader, filter_loader):
    try:
        loader.get('origin_probe')
    except AnsibleError:
        pass
    else:
        raise AssertionError('cached user plugin accepted')
assert all(str(project) not in plugin._original_path for plugin in filter_loader.all())
''')
        self.assert_success(result)

    def test_collection_metadata_cannot_redirect_to_user_python_code(self):
        self.plugin('project/origin_redirect_payload.py', '')
        self.write('project/collections/ansible_collections/origin_tests/redirect/meta/runtime.yml',
                   'import_redirection:\n  ansible_collections.origin_tests.redirect.plugins:\n'
                   '    redirect: origin_redirect_payload\n')
        result = self.run_python('''
from importlib import import_module
from ansible.plugins.loader import init_plugin_loader
from ansible.utils.collection_loader import AnsibleCollectionConfig
sys.path.insert(0, str(project))
init_plugin_loader()
AnsibleCollectionConfig.playbook_paths = [str(project)]
try:
    import_module('ansible_collections.origin_tests.redirect.plugins')
except ImportError:
    pass
else:
    raise AssertionError('collection redirect executed user code')
assert not (root / 'imported').exists()
''')
        self.assert_success(result)
        self.assertFalse(self.marker.exists())

    def test_collection_initializers_from_playbook_and_pythonpath_are_rejected(self):
        self.plugin('project/collections/ansible_collections/origin_tests/local/plugins/__init__.py', '')
        for discovery in ('playbook', 'pythonpath'):
            with self.subTest(discovery=discovery):
                result = self.run_python('''
from importlib import import_module
from ansible.plugins.loader import init_plugin_loader
from ansible.utils.collection_loader import AnsibleCollectionConfig
if %r == 'pythonpath':
    sys.path.insert(0, str(project / 'collections'))
init_plugin_loader()
if %r == 'playbook':
    AnsibleCollectionConfig.playbook_paths = [str(project)]
try:
    import_module('ansible_collections.origin_tests.local.plugins')
except ImportError as e:
    assert 'User-supplied Ansible plugin code is disabled' in str(e), str(e)
else:
    raise AssertionError('project collection initializer executed')
''' % (discovery, discovery))
                self.assert_success(result)
                self.assertFalse(self.marker.exists())

    def test_configured_collection_works_but_symlink_escape_does_not(self):
        self.plugin('installed/ansible_collections/origin_tests/trusted/plugins/action/origin_probe.py',
                    'from ansible.plugins.action import ActionBase\nclass ActionModule(ActionBase): pass\n')
        self.plugin('outside.py', 'from ansible.plugins.action import ActionBase\nclass ActionModule(ActionBase): pass\n')
        self.plugin('installed-extra/ansible_collections/origin_tests/trusted/plugins/action/origin_probe.py', '')
        script = '''
from ansible.errors import AnsibleError
from ansible.plugins.loader import action_loader, init_plugin_loader
init_plugin_loader([str(root / 'installed')])
%s
'''
        self.assert_success(self.run_python(script % "assert action_loader.get('origin_tests.trusted.origin_probe', class_only=True)"))
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        plugin = self.root / 'installed/ansible_collections/origin_tests/trusted/plugins/action/origin_probe.py'
        plugin.unlink()
        plugin.symlink_to(self.root / 'outside.py')
        self.assert_success(self.run_python(script % '''
try:
    action_loader.get('origin_tests.trusted.origin_probe', class_only=True)
except AnsibleError:
    pass
else:
    raise AssertionError('collection symlink escape accepted')
from ansible.utils.collection_loader._collection_finder import _is_trusted_plugin_source
assert not _is_trusted_plugin_source(root / 'installed-extra/ansible_collections/origin_tests/trusted/plugins/action/origin_probe.py')
'''))
        self.assertFalse(self.marker.exists())

    def test_installed_runner_callbacks_remain_usable(self):
        self.plugin('installed/ansible_runner/display_callback/callback/origin_probe.py',
                    'from ansible.plugins.callback import CallbackBase\nclass CallbackModule(CallbackBase): pass\n')
        result = self.run_python('''
from unittest.mock import patch
from ansible.plugins.loader import callback_loader, init_plugin_loader
with patch('site.getsitepackages', return_value=[str(root / 'installed')]):
    init_plugin_loader()
callback_loader.add_directory(str(root / 'installed/ansible_runner/display_callback/callback'))
assert callback_loader.get('origin_probe', class_only=True)
from ansible.utils.collection_loader._collection_finder import _is_trusted_plugin_source
assert not _is_trusted_plugin_source(root / 'installed/other_package/plugin.py')
''')
        self.assert_success(result)
        self.assertTrue(self.marker.exists())

    def test_fetch_cannot_create_plugins_for_later_roles(self):
        payload = self.plugin('remote/origin_probe.py',
                              'from ansible.plugins.action import ActionBase\n'
                              'class ActionModule(ActionBase):\n'
                              '    def run(self, tmp=None, task_vars=None): return {"changed": False}\n')
        self.write('project/run.yml', '- origin_probe: {}\n')
        for location, trusted in (('project/generated', False), ('outside/generated', False), ('project/trusted', True)):
            role = self.root / location
            dest = role / 'action_plugins/origin_probe.py'
            self.write('project/main.yml', '- hosts: all\n  gather_facts: false\n  tasks:\n'
                       '    - fetch: {src: %s, dest: %s, flat: true}\n'
                       '    - include_role: {name: %s}\n'
                       '    - include_tasks: run.yml\n' % (json.dumps(str(payload)), json.dumps(str(dest)), json.dumps(str(role))))
            with self.subTest(location=location, trusted=trusted):
                result = self.run_python(LOCAL_CLI, trusted=trusted)
                self.assertTrue(dest.exists(), result.stdout + result.stderr)
                if trusted:
                    self.assert_success(result)
                    self.assertTrue(self.marker.exists())
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('User-supplied Ansible plugin code is disabled', result.stdout + result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_fetch_cannot_overwrite_trusted_plugin_sources(self):
        source = self.write('remote/payload.txt', 'replacement')
        installed = self.write('installed/ansible_collections/origin_tests/demo/plugins/action/probe.py', 'original')
        script = "os.environ['ANSIBLE_COLLECTIONS_PATH'] = str(root / 'installed')\n" + LOCAL_CLI
        for dest, trusted, allowed in ((installed, False, False), (self.root / 'download.txt', False, True), (installed, True, True)):
            self.write('project/main.yml', '- hosts: all\n  gather_facts: false\n  tasks:\n'
                       '    - fetch: {src: %s, dest: %s, flat: true}\n' % (json.dumps(str(source)), json.dumps(str(dest))))
            with self.subTest(dest=str(dest), trusted=trusted):
                result = self.run_python(script, trusted=trusted)
                if allowed:
                    self.assert_success(result)
                    self.assertEqual(dest.read_text(), 'replacement')
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('Cannot overwrite trusted Ansible plugin sources', result.stdout + result.stderr)
                    self.assertEqual(installed.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
