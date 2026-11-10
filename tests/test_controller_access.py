"""Run with PYTHONPATH=lib python -m unittest discover -s tests."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from ansible.errors import AnsibleError
from ansible.parsing.dataloader import DataLoader
from ansible.playbook import Playbook
from ansible.playbook.task import Task
from ansible.plugins.action import ActionBase
from ansible.plugins.action.copy import ActionModule as CopyAction
from ansible.plugins.action.include_vars import ActionModule as IncludeVarsAction
from ansible.plugins.action.template import ActionModule as TemplateAction
from ansible.plugins.loader import init_plugin_loader, lookup_loader
from ansible.template import Templar


class ControllerAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_plugin_loader()

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.outside = self.root / 'outside.yml'
        self.outside.write_text('value: OUTSIDE_MARKER\n')
        self.loader = DataLoader()
        self.loader.set_basedir(str(self.project))
        self.loader._playbook_root = str(self.project)
        self.templar = Templar(self.loader)
        self.environment = patch.dict(os.environ, {'ANSIBLE_SUPER_MODE': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, name, text):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def action(self, cls, name, args):
        return cls(
            task=Task.load({name: args}, loader=self.loader), connection=Mock(),
            play_context=Mock(), loader=self.loader, templar=self.templar,
            shared_loader_obj=Mock(),
        )

    def test_lookup_and_query_block_files_for_all_names(self):
        for call in ('lookup', 'query', 'q'):
            for plugin in ('file', 'template', 'pipe', 'env', 'url', 'first_found'):
                for prefix in ('', 'ansible.builtin.', 'ansible.legacy.'):
                    expression = "{{ %s('%s', '%s') }}" % (call, prefix + plugin, self.outside)
                    with self.subTest(expression=expression), self.assertRaisesRegex(AnsibleError, 'disabled'):
                        self.templar.template(expression)

    def test_safe_names_cannot_select_third_party_or_shadow_plugins(self):
        plugin_dir = self.project / 'lookup_plugins'
        self.write('lookup_plugins/items.py', 'raise AssertionError("custom lookup imported")\n')
        lookup_loader.add_directory(str(plugin_dir))
        for name in ('items', 'ansible.builtin.items', 'ansible.legacy.items'):
            instance = lookup_loader.get(name, loader=self.loader, templar=self.templar)
            self.assertEqual(instance.run([1, 2]), [1, 2])
            self.assertNotIn(str(plugin_dir), instance._original_path)
        for name in ('evil.collection.items', 'ansible.builtin.evil.items', 'cartesian', ['items']):
            with self.subTest(name=name), patch.object(lookup_loader, 'find_plugin_with_context') as resolve:
                with self.assertRaises(AnsibleError):
                    lookup_loader.get(name, loader=self.loader, templar=self.templar)
                resolve.assert_not_called()

    def test_safe_data_lookups_and_expressions_still_work(self):
        templar = Templar(self.loader, variables={'value': 'hello'})
        self.assertEqual(templar.template("{{ query('items', [1, 2]) }}"), [1, 2])
        self.assertEqual(templar.template("{{ lookup('vars', 'value') }}"), 'hello')
        self.assertEqual(templar.template('{{ 1 + 2 }}'), '3')

    def test_file_paths_reject_parent_prefix_and_symlink_escapes(self):
        inside = self.write('files/data.txt', 'inside')
        sibling = self.root / 'project-extra'
        sibling.mkdir()
        (sibling / 'data.txt').write_text('outside')
        link = self.project / 'files/link.txt'
        link.symlink_to(self.outside)
        self.assertEqual(self.loader.get_real_file(str(inside)), str(inside))
        for name in (str(self.outside), '../outside.yml', str(link), str(sibling / 'data.txt')):
            with self.subTest(name=name), self.assertRaisesRegex(AnsibleError, 'outside the playbook project'):
                self.loader.get_real_file(name)
        self.loader.set_basedir(str(self.root))
        with self.assertRaises(AnsibleError):
            self.loader.get_real_file('outside.yml')

    def test_resource_search_checks_user_paths(self):
        inside = self.write('files/data.txt', 'inside')
        self.assertEqual(self.loader.path_dwim_relative_stack([str(self.project)], 'files', 'data.txt'), str(inside))
        with self.assertRaises(AnsibleError):
            self.loader.path_dwim_relative_stack([str(self.project)], 'files', str(self.outside))

    def test_inventory_and_internal_data_reads_are_not_globally_restricted(self):
        self.assertEqual(self.loader.load_from_file(str(self.outside)), {'value': 'OUTSIDE_MARKER'})

    def test_template_includes_preserve_project_files_and_reject_escapes(self):
        self.write('part.j2', 'hello {{ value }}')
        templar = Templar(self.loader, variables={'value': 'world'})
        self.assertEqual(templar.template("{% include 'part.j2' %}"), 'hello world')
        (self.project / 'link.j2').symlink_to(self.outside)
        with self.assertRaisesRegex(AnsibleError, 'outside the playbook project'):
            templar.template("{% include 'link.j2' %}")
        outside_search = templar.copy_with_new_env(searchpath=[str(self.root)])
        with self.assertRaisesRegex(AnsibleError, 'outside the playbook project'):
            outside_search.template("{% include 'outside.yml' %}")

    def test_copy_content_tempfile_is_allowed_without_allowing_all_temp_files(self):
        action = self.action(CopyAction, 'copy', {'content': 'hello', 'dest': '/tmp/example'})
        filename = action._create_content_tempfile('hello')
        try:
            self.assertEqual(self.loader.get_real_file(filename), filename)
            with self.assertRaises(AnsibleError):
                self.loader.get_real_file(str(self.outside))
        finally:
            action._remove_tempfile_if_content_defined('hello', filename)
        self.assertNotIn(os.path.realpath(filename), self.loader._tempfiles)

    def test_template_generated_file_passes_to_copy_and_is_removed_afterwards(self):
        self.write('templates/main.j2', 'hello {{ value }}')
        action = self.action(TemplateAction, 'template', {'src': 'main.j2', 'dest': '/remote/result'})
        generated = []

        def get_copy(name, **kwargs):
            copy = CopyAction(**kwargs)
            copy._play_context.check_mode = True
            copy._play_context.diff = False
            copy._play_context.no_log = False
            copy._connection._shell.path_has_trailing_slash.side_effect = lambda name: name.endswith('/')
            copy._remote_expand_user = lambda name: name
            copy._execute_remote_stat = Mock(return_value={'exists': False, 'checksum': None})
            copy._remove_tmp_path = Mock()
            filename = copy._task.args['src']
            self.assertEqual(Path(filename).read_text(), 'hello world')
            generated.append(filename)
            return copy

        action._shared_loader_obj.action_loader.get.side_effect = get_copy
        action._remove_tmp_path = Mock()
        with patch.object(ActionBase, 'run', return_value={}):
            result = action.run(task_vars={'value': 'world'})
        self.assertTrue(result['changed'], result)
        self.assertEqual(len(generated), 1)
        self.assertFalse(Path(generated[0]).exists())
        self.assertFalse(self.loader._tempfiles)

    def test_include_vars_file_and_directory_are_checked(self):
        self.write('vars/local.yml', 'value: inside\n')
        with patch.object(ActionBase, 'run', return_value={}):
            result = self.action(IncludeVarsAction, 'include_vars', {'file': 'local.yml'}).run(task_vars={})
        self.assertEqual(result['ansible_facts'], {'value': 'inside'})
        action = self.action(IncludeVarsAction, 'include_vars', {'dir': str(self.root)})
        with self.assertRaisesRegex(AnsibleError, 'outside the playbook project'):
            action.run(task_vars={})

    def test_playbook_import_cannot_expand_the_project_root(self):
        main = self.write('main.yml', '- import_playbook: ../outside.yml\n')
        with self.assertRaisesRegex(AnsibleError, 'outside the playbook project'):
            Playbook.load(str(main), loader=self.loader)
        self.assertEqual(self.loader._playbook_root, str(self.project))

    def test_trusted_automation_retains_external_files_and_lookups(self):
        with patch.dict(os.environ, {'ANSIBLE_SUPER_MODE': '1'}):
            self.assertEqual(self.loader.get_real_file(str(self.outside)), str(self.outside))
            expression = "{{ lookup('template', '%s') }}" % self.outside
            self.assertEqual(str(self.templar.template(expression)), self.outside.read_text())

    def test_cli_loop_and_task_import_paths(self):
        # A local fixture transport is enabled only during connection construction.
        # The executor, template engine and actions still run in restricted mode.
        script = '''
import sys
from unittest.mock import patch
from ansible.plugins.connection.local import Connection
original = Connection.__init__
def initialize(self, *args, **kwargs):
    with patch.dict('os.environ', {'ANSIBLE_SUPER_MODE': '1'}):
        original(self, *args, **kwargs)
Connection.__init__ = initialize
from ansible.cli.playbook import main
main(['ansible-playbook', '-i', 'fixture_host,', '-c', 'local', sys.argv[1]])
'''
        self.write('tasks/inside.yml', '- debug: {msg: INSIDE_MARKER}\n')
        self.write('files/hello.sh', '#!/bin/sh\nprintf SCRIPT_MARKER\n')
        self.write('roles/demo/tasks/main.yml', '- debug: {msg: ROLE_MARKER}\n')
        external_role = self.root / 'external_role'
        (external_role / 'tasks').mkdir(parents=True)
        (external_role / 'tasks/main.yml').write_text('- debug: {msg: OUTSIDE_MARKER}\n')
        fixtures = (
            ('safe-loop', '    - debug: {msg: "{{ item }}"}\n      with_items: [one, two]\n', 0),
            ('file-loop', '    - debug: {msg: "{{ item }}"}\n      with_file: "%s"\n' % self.outside, 2),
            ('inside-import', '    - import_tasks: tasks/inside.yml\n', 0),
            ('outside-import', '    - import_tasks: "%s"\n' % self.outside, 4),
            ('outside-include', '    - include_tasks: "%s"\n' % self.outside, 4),
            ('normal-script', '    - script: files/hello.sh\n', 0),
            ('inside-role', '    - include_role: {name: demo}\n', 0),
            ('outside-role', '    - include_role: {name: "%s"}\n' % external_role, 2),
            ('outside-copy', '    - copy: {src: "%s", dest: /tmp/example}\n' % self.outside, 2),
            ('outside-template', '    - template: {src: "%s", dest: /tmp/example}\n' % self.outside, 2),
            ('outside-script', '    - script: "%s"\n' % self.outside, 2),
        )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'lib'),
                   ANSIBLE_LOCAL_TEMP=str(self.root / 'ansible-tmp'),
                   ANSIBLE_REMOTE_TEMP=str(self.root / 'remote-tmp'), ANSIBLE_NOCOLOR='1')
        for name, tasks, expected in fixtures:
            playbook = self.write(name + '.yml', '- hosts: all\n  gather_facts: false\n  tasks:\n' + tasks)
            result = subprocess.run([sys.executable, '-c', script, str(playbook)], cwd=self.project,
                                    env=env, capture_output=True, text=True, timeout=30)
            with self.subTest(name=name):
                self.assertEqual(result.returncode == 0, expected == 0, result.stdout + result.stderr)
                if expected:
                    self.assertRegex(result.stdout + result.stderr, 'disabled|outside the playbook project')
                self.assertNotIn('OUTSIDE_MARKER', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
