"""Narrow captured-workspace context on the existing actions.invoke contract."""
from dataclasses import dataclass
import re

WORKSPACE_LAYOUT_ENTRY = 'trigger.toggle.workspace-layout'
LAYOUTS = frozenset({'dwindle','scrolling'})
_EFFECT_KEYS = {'workspace_id','from_layout','layout','runtime_applied','persistent_applied','readback_confirmed','status','code'}

class WorkspaceActionError(ValueError):
    def __init__(self,code='invalid_workspace_request',status=400):
        self.code,self.status=code,status
        super().__init__(code)

def valid_layout(value):return isinstance(value,str) and value in LAYOUTS

def validate_workspace_request(params,state_revision,target_token=None):
    if (not isinstance(params,dict) or set(params)!={'workspace_id','from_layout','layout'}
            or type(params['workspace_id']) is not int or not 1<=params['workspace_id']<=10
            or not valid_layout(params['from_layout']) or not valid_layout(params['layout'])
            or params['from_layout']==params['layout']
            or type(state_revision) is not int or not 0<=state_revision<2**53
            or target_token is not None):
        raise WorkspaceActionError()
    return WorkspaceActionContext(params['workspace_id'],state_revision,params['from_layout'],params['layout'])

@dataclass(frozen=True)
class WorkspaceActionContext:
    workspace_id: int
    state_revision: int
    from_layout: str
    layout: str


def validate_workspace_effects(value,context):
    """Validate metadata only; false booleans mean unconfirmed, not no mutation."""
    if (not isinstance(value,dict) or set(value)!=_EFFECT_KEYS
            or type(value['workspace_id']) is not int or value['workspace_id']!=context.workspace_id
            or value['from_layout']!=context.from_layout or value['layout']!=context.layout
            or any(type(value[key]) is not bool for key in ('runtime_applied','persistent_applied','readback_confirmed'))
            or not isinstance(value['status'],str) or value['status'] not in {'applied','partial','failed'}
            or not isinstance(value['code'],str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}',value['code'])):
        raise WorkspaceActionError('workspace_layout_outcome_unknown',503)
    complete=all(value[key] for key in ('runtime_applied','persistent_applied','readback_confirmed'))
    if (value['status']=='applied') != complete:
        raise WorkspaceActionError('workspace_layout_outcome_unknown',503)
    return dict(value)


def validate_workspace_binding(value):
    if (not isinstance(value,dict) or set(value)!={'revision','instance_id'}
            or type(value['revision']) is not int or not 1<=value['revision']<2**53
            or not isinstance(value['instance_id'],str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',value['instance_id'])):
        raise WorkspaceActionError('invalid_workspace_binding',400)
    return dict(value)
