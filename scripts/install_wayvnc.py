#!/usr/bin/env python3
"""Install the explicit optional VNC dependency using the official Arch repo.
No listener/output/service is started; default Sunshine is unchanged.
"""
import argparse
import json
import re
import subprocess

class WayVNCInstallError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

def install_dependency(*,install=False,runner=subprocess.run):
    def run(argv):
        try:return runner(argv,capture_output=True,text=True,check=False,timeout=180)
        except (OSError,subprocess.SubprocessError):raise WayVNCInstallError('wayvnc_package_manager_unavailable') from None
    installed=run(['/usr/bin/pacman','-Q','wayvnc'])
    if installed.returncode==0:
        parts=installed.stdout.strip().split()
        version=parts[1] if len(parts)==2 else None
        if version and version.startswith('0.10.1-'):
            return {'available':True,'version':version,'installed':True,'changed':False,'started':False}
        raise WayVNCInstallError('wayvnc_installed_version_unsupported')
    info=run(['/usr/bin/pacman','-Si','wayvnc'])
    match=re.search(r'^Version\s*:\s*(\S+)',info.stdout,re.M)
    version=match[1] if match else None
    if info.returncode or not version or not version.startswith('0.10.1-'):
        raise WayVNCInstallError('wayvnc_0_10_1_repository_package_required')
    if not install:return {'available':True,'version':version,'installed':False,'changed':False,'started':False}
    result=run(['/usr/bin/sudo','-n','/usr/bin/pacman','-S','--needed','--noconfirm','wayvnc'])
    if result.returncode:raise WayVNCInstallError('wayvnc_package_install_failed')
    verified=run(['/usr/bin/pacman','-Q','wayvnc'])
    if verified.returncode or verified.stdout.strip().split()!=['wayvnc',version]:raise WayVNCInstallError('wayvnc_install_readback_failed')
    return {'available':True,'version':version,'installed':True,'changed':True,'started':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--install',action='store_true');args=parser.parse_args()
    try:print(json.dumps(install_dependency(install=args.install)));return 0
    except WayVNCInstallError as error:print(json.dumps({'available':False,'reason':error.code,'started':False}));return 1
if __name__=='__main__':raise SystemExit(main())
