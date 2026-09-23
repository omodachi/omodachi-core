"""Bounded plugin action helper over the existing authenticated IPC operation.

Only catalog IDs, revisions, declared parameters and explicit displayed targets
are forwarded. The helper never runs a returned argv or copies credentials into
QML/stdout. Existing server routing/confirmation and idempotence remain decisive.
"""
import asyncio
import json
import re
from .ipc import JsonLineClient
from .plugin_bridge import plugin_credential, BridgeError
from .workspace_actions import (WORKSPACE_LAYOUT_ENTRY, WorkspaceActionError, validate_workspace_request, validate_workspace_effects, validate_workspace_binding)

_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z')
_CODE=re.compile(r'[a-z][a-z0-9_]{0,95}\Z')

def failure(code):return {'ok':False,'error':code,'message':code}

def parameters(text):
    if not isinstance(text,str) or len(text.encode())>8192:raise ValueError('invalid_request')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('invalid_request')
            result[key]=value
        return result
    value=json.loads(text,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid_request')))
    if not isinstance(value,dict) or len(value)>32:raise ValueError('invalid_request')
    return value

async def invoke_plugin_action(socket_path, *, entry_id, catalog_revision, request_id, params=None,
                               state_revision=None, target_token=None, workspace_revision=None, workspace_instance=None, credential_loader=plugin_credential):
    if (not isinstance(entry_id,str) or not _ID.fullmatch(entry_id)
            or not isinstance(request_id,str) or not _ID.fullmatch(request_id)
            or not isinstance(catalog_revision,str) or not 1<=len(catalog_revision)<=128
            or not _ID.fullmatch(catalog_revision) or not isinstance(params if params is not None else {},dict)):
        return failure('invalid_request')
    workspace_context=None
    proof=None
    if workspace_revision is not None or workspace_instance is not None:
        if entry_id!=WORKSPACE_LAYOUT_ENTRY:return failure('invalid_workspace_binding')
        try:proof=validate_workspace_binding({'revision':workspace_revision,'instance_id':workspace_instance})
        except WorkspaceActionError as error:return failure(error.code)
    if entry_id==WORKSPACE_LAYOUT_ENTRY:
        try:workspace_context=validate_workspace_request(params,state_revision,target_token)
        except WorkspaceActionError as error:return failure(error.code)
    elif (state_revision is None)!=(target_token is None):return failure('invalid_request')
    payload={'entry_id':entry_id,'request_id':request_id,'catalog_revision':catalog_revision,'params':params or {}}
    if proof is not None:payload['workspace_binding']=proof
    if state_revision is not None:
        if type(state_revision) is not int or not 0<=state_revision<2**53:return failure('invalid_request')
        payload['state_revision']=state_revision
        if workspace_context is None:
            if not isinstance(target_token,str) or not _ID.fullmatch(target_token):return failure('invalid_request')
            payload['target_token']=target_token
    try:
        if len(json.dumps(payload,allow_nan=False).encode())>16384:return failure('invalid_request')
        token=credential_loader()
        response=await JsonLineClient(socket_path,token,timeout=6).request('actions.invoke',**payload)
    except BridgeError as error:
        return failure(error.code)
    except (OSError,asyncio.TimeoutError):
        return failure('daemon_unavailable')
    except (ValueError,TypeError):
        return failure('invalid_request')
    if not isinstance(response,dict):return failure('invalid_response')
    if response.get('ok') is not True:
        for candidate in (response.get('error'),response.get('message')):
            if isinstance(candidate,str) and _CODE.fullmatch(candidate):return failure(candidate)
        return failure('action_rejected')
    result=response.get('result')
    if (not isinstance(result,dict) or result.get('request_id')!=request_id or result.get('entry_id')!=entry_id
            or result.get('status') not in {'accepted','prepared','failed'}):return failure('invalid_response')
    route=result.get('route')
    if not isinstance(route,dict) or route.get('route') not in {'host','terminal','desktop','native'}:
        return failure('invalid_response')
    # A terminal/native prepared descriptor is not execution. Do not forward
    # argv/command for QML to execute or invent a surface completion receipt.
    safe_route={key:route[key] for key in ('route','supported','entry_id','native_view') if key in route}
    value={key:result[key] for key in ('contract_revision','request_id','entry_id','status') if key in result}
    value['route']=safe_route
    # PERF-5. The revision core actually resolved this call against. The
    # plugin holds it so its next press is optimistic about a newer catalog
    # than the one its last read gave it, not the same stale one.
    resolved=result.get('catalog_revision')
    if isinstance(resolved,str) and 1<=len(resolved)<=128 and _ID.fullmatch(resolved):
        value['catalog_revision']=resolved
    if workspace_context is not None:
        if 'workspace_effects' in result:
            try:value['workspace_effects']=validate_workspace_effects(result['workspace_effects'],workspace_context)
            except WorkspaceActionError:return failure('invalid_response')
            if (result['status']=='accepted') != (value['workspace_effects']['status']=='applied'):
                return failure('invalid_response')
        elif result['status']=='accepted':return failure('invalid_response')
        if result['status']=='failed':
            code=result.get('code','workspace_layout_outcome_unknown')
            value['code']=code if isinstance(code,str) and _CODE.fullmatch(code) else 'workspace_layout_outcome_unknown'
    elif result['status']=='failed':value['code']='action_failed' 
    return {'ok':True,'result':value}
