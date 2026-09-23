from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from install_wayvnc import install_dependency,WayVNCInstallError

class WayVNCInstallTests(unittest.TestCase):
    def test_explicit_setup_installs_repo_package_and_reads_back_without_start(self):
        commands=[];installed=[False]
        def run(argv,**kwargs):
            commands.append(argv)
            if argv[1]=='-Q':return SimpleNamespace(returncode=0 if installed[0] else 1,stdout='wayvnc 0.10.1-2' if installed[0] else '')
            if argv[1]=='-Si':return SimpleNamespace(returncode=0,stdout='Version         : 0.10.1-2\n')
            self.assertEqual(argv[:5],['/usr/bin/sudo','-n','/usr/bin/pacman','-S','--needed']);installed[0]=True
            return SimpleNamespace(returncode=0,stdout='')
        self.assertFalse(install_dependency(install=False,runner=run)['installed']);self.assertFalse(installed[0])
        result=install_dependency(install=True,runner=run);self.assertTrue(result['changed']);self.assertFalse(result['started'])
        before=len(commands);result=install_dependency(install=True,runner=run);self.assertFalse(result['changed']);self.assertEqual(len(commands),before+1)
    def test_unreviewed_repository_version_does_not_install(self):
        commands=[]
        def run(argv,**kwargs):
            commands.append(argv);return SimpleNamespace(returncode=1 if argv[1]=='-Q' else 0,stdout='Version : 0.11.0-1')
        with self.assertRaisesRegex(WayVNCInstallError,'repository_package_required'):install_dependency(install=True,runner=run)
        self.assertFalse(any(c[0].endswith('sudo') for c in commands))
