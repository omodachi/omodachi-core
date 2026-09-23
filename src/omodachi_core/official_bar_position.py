"""Session-scoped ownership of the official, globally positioned Omarchy bar.

Only `omarchy bar position ENUM` changes shell configuration. The position is
read at commit/end, never polled or periodically forced. User overrides win.
"""
from __future__ import annotations
import json
from pathlib import Path
import subprocess

POSITIONS={'top','bottom','left','right'}

class OfficialBarPosition:
    def __init__(self, *, home=None, read_position=None, set_position=None):
        self.home=Path(home) if home is not None else Path.home()
        self.read_position=read_position or self._read_position
        self.set_position=set_position or self._set_position

    def _read_position(self):
        path=self.home/'.config/omarchy/shell.json'
        if not path.exists():path=Path('/usr/share/omarchy/config/omarchy/shell.json')
        with path.open('rb') as source:raw=source.read(65537)
        if len(raw)>65536:raise ValueError('bar_config_unavailable')
        value=json.loads(raw).get('bar',{})
        if value.get('id','omarchy.bar')!='omarchy.bar':raise ValueError('official_bar_not_selected')
        position=value.get('position','top')
        if position not in POSITIONS:raise ValueError('bar_position_unavailable')
        return position

    def _set_position(self,position):
        if position not in POSITIONS:raise ValueError('bar_position_unavailable')
        from .graphical import graphical_environment
        env=graphical_environment();env['OMARCHY_PATH']='/usr/share/omarchy'
        result=subprocess.run(['/usr/bin/omarchy','bar','position',position],env=env,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3,check=False)
        if result.returncode:raise ValueError('official_bar_command_failed')

    def _preferences(self):
        result={'landscape':'top','portrait':'left'}
        path=self.home/'.config/omarchy/omodachi-remote-bar.json'
        try:
            with path.open('rb') as source:raw=source.read(4097)
            value=json.loads(raw) if len(raw)<=4096 else {}
            for key,allowed in [('landscape',{'top','bottom'}),('portrait',{'left','right'})]:
                if isinstance(value,dict) and value.get(key) in allowed:result[key]=value[key]
        except (OSError,ValueError,TypeError):pass
        return result

    def committed(self,journal,viewport):
        record=journal.read()
        if not record or record.get('state')=='removed':return {'status':'inactive','scope':'global'}
        try:
            current=self.read_position()
            owned=record.get('official_bar_position')
            if owned is None:
                owned={'original_position':current,'last_owned_position':current,'state':'owned',
                       'preferences':self._preferences(),'orientation':None}
                record['official_bar_position']=owned
            if owned['state']=='user_override':return {'status':'user_override','scope':'global'}
            if current!=owned['last_owned_position']:
                owned['state']='user_override';journal.write(record)
                return {'status':'user_override','scope':'global'}
            orientation='portrait' if viewport['height']>viewport['width'] else 'landscape'
            desired=owned['preferences'][orientation]
            if owned.get('orientation')==orientation:return {'status':'unchanged','scope':'global','position':current}
            owned['pending_position']=desired;journal.write(record)
            if current!=desired:self.set_position(desired)
            if self.read_position()!=desired:raise ValueError('bar_position_unconfirmed')
            owned.update(last_owned_position=desired,orientation=orientation,state='owned');owned.pop('pending_position',None)
            journal.write(record)
            return {'status':'applied','scope':'global','position':desired}
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            return {'status':'unavailable','scope':'global'}

    def restore(self,journal):
        record=journal.read();owned=record.get('official_bar_position') if record else None
        if not owned or owned.get('state') in {'restored','user_override'}:return {'status':'unchanged','scope':'global'}
        try:
            current=self.read_position()
            if current not in {owned['last_owned_position'],owned.get('pending_position')}:
                owned['state']='user_override';journal.write(record)
                return {'status':'user_override','scope':'global'}
            if current!=owned['original_position']:self.set_position(owned['original_position'])
            if self.read_position()!=owned['original_position']:raise ValueError('bar_restore_unconfirmed')
            owned['state']='restored';owned.pop('pending_position',None);journal.write(record)
            return {'status':'restored','scope':'global','position':owned['original_position']}
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            return {'status':'unavailable','scope':'global'}
