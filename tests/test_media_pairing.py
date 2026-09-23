"""Temporary Unix peer + synthetic GameStream states; never real Sunshine/pairs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import uuid

from omodachi_core.media_pairing import (MediaPairingBridge, MediaPairingStore,
    SunshinePairingIPC, MediaPairingError)


class Clock:
    value=1000.0
    def __call__(self):return self.value


class FakeFork:
    def __init__(self,revocation=False):
        self.rows={};self.calls=[];self.approvals=0;self.fail_cancel=False;self.after_approve=None
        # A fork before PLUG-4 has no persistent revoke at all and answers
        # `unknown_operation`; `certs` is the authorized certificate store a
        # fork that does have one takes a revoke out of.
        self.revocation=revocation;self.certs=set();self.fail_revoke=False
        # What the store would show for a certificate: its display name. A
        # record with no name is exactly the unnamed leftover PLUG-4 found.
        self.names={};self.unreadable=0
    def add(self,fp,upper=True):
        # The real managed fork emits its request IDs in UPPERCASE, so that is
        # what this double does by default.
        rid=str(uuid.uuid4());rid=rid.upper() if upper else rid
        self.rows[rid]={'request_id':rid,'client_cert_sha256':fp,'status':'pending','expires_in_ms':120000};return rid
    def request(self,op,**fields):
        # Store only the shape, never the PIN, even in fixture diagnostics.
        self.calls.append((op,{key:value for key,value in fields.items() if key!='pin'}))
        if op=='pairing.list':
            return {'requests':deepcopy(list(self.rows.values())),
                    'certificate_revocation_supported':self.revocation}
        if op=='pairing.clients':
            return {'clients':[{'client_cert_sha256':fp,'name':self.names.get(fp,''),'enabled':True}
                               for fp in sorted(self.certs)],
                    'unreadable':self.unreadable,'truncated':False}
        if op=='pairing.revoke':
            if not self.revocation:raise MediaPairingError('unknown_operation')
            if self.fail_revoke:raise MediaPairingError('media_pairing_ipc_unavailable',503)
            fp=fields['client_cert_sha256']
            found=fp in self.certs;self.certs.discard(fp)
            for row in self.rows.values():
                if row['client_cert_sha256']==fp and row['status'] in {'pending','awaiting_client_proof'}:
                    row['status']='cancelled'
            return {'status':'revoked' if found else 'not_found'}
        row=self.rows.get(fields.get('request_id'))
        if not row:raise MediaPairingError('pairing_request_not_pending')
        if row['client_cert_sha256']!=fields.get('client_cert_sha256'):raise MediaPairingError('pairing_binding_mismatch')
        if op=='pairing.pending':return {'request':deepcopy(row)}
        if op=='pairing.cancel':
            if self.fail_cancel:raise MediaPairingError('media_pairing_ipc_unavailable',503)
            if row['status']=='paired':raise MediaPairingError('pairing_request_not_pending')
            row['status']='cancelled';return {'request':deepcopy(row)}
        if op=='pairing.approve':
            if row['status']!='pending':raise MediaPairingError('pairing_pin_already_submitted')
            assert isinstance(fields['pin'],str) and len(fields['pin'])==4
            self.approvals+=1;row['status']='awaiting_client_proof';self.certs.add(row['client_cert_sha256'])
            if self.after_approve:self.after_approve()
            return {'accepted':True,'request_id':row['request_id'],'status':'awaiting_client_proof'}
        raise AssertionError('invented private operation')


class MediaBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.clock=Clock();self.fork=FakeFork()
        self.store=MediaPairingStore(self.root/'private'/'state.json')
        self.bridge=MediaPairingBridge(self.fork,self.store,clock=self.clock,monotonic=self.clock)
        self.valid=True
        self.auth=lambda:self.valid
        self.local=lambda:True
        self.fp='a'*64;self.rid=self.fork.add(self.fp)
        self.pin='7391'
    def tearDown(self):self.tmp.cleanup()
    def payload(self,rid=None,fp=None):return {'request_id':rid or self.rid,'client_cert_sha256':fp or self.fp,'pin':self.pin}
    def grant(self,device='ipad'):
        return self.bridge.grant_remote(device,remote_allowed=True,source_request_id='pair_'+'1'*32,local_authorize=self.local)
    def submit(self,device='ipad',rid=None,fp=None):return self.bridge.submit(device,self.payload(rid,fp),authorize=self.auth)
    def state(self):
        with self.store.transaction() as state:return deepcopy(state)
    def test_the_forks_uppercase_request_id_is_carried_through_unchanged(self):
        """The managed fork answers `pairing.list` with UPPERCASE request IDs.

        A lowercase-only pattern rejected the whole list the moment it held a
        real pending request, so the bridge failed with
        `media_pairing_invalid_binding` before the PIN could ever be submitted.
        """
        upper = self.fork.add('c'*64)
        self.assertEqual(upper, upper.upper())
        # A lowercase ID is still accepted; only the case restriction is gone.
        lower = self.fork.add('d'*64, upper=False)
        self.assertEqual(self.bridge.discover('ipad', 'd'*64, authorize=self.auth,
                                              pairing_intent=True)['request_id'], lower)
        found = self.bridge.discover('ipad', 'c'*64, authorize=self.auth, pairing_intent=True)
        self.assertEqual(found['request_id'], upper)
        self.grant()
        attempt = self.bridge.submit('ipad', {'request_id': upper, 'client_cert_sha256': 'c'*64, 'pin': self.pin},
                                     authorize=self.auth)
        self.assertEqual(attempt['request_id'], upper)
        self.assertEqual(attempt['status'], 'awaiting_client_proof')
        # Everything sent back to the fork keeps the fork's own spelling.
        for operation, fields in self.fork.calls:
            if 'request_id' in fields:
                self.assertEqual(fields['request_id'], upper, operation)

    def test_discover_returns_only_unique_exact_fingerprint_without_grant(self):
        with self.assertRaisesRegex(MediaPairingError,'intent_required'):
            self.bridge.discover('ipad',self.fp,authorize=self.auth)
        otherfp='b'*64;self.fork.add(otherfp)
        result=self.bridge.discover('ipad',self.fp,authorize=self.auth,pairing_intent=True)
        self.assertEqual(result['request_id'],self.rid);self.assertEqual(result['client_cert_sha256'],self.fp)
        self.assertFalse(self.state()['devices']);self.assertEqual(self.fork.approvals,0)
        self.fork.add(self.fp)
        with self.assertRaisesRegex(MediaPairingError,'not_unique'):self.bridge.discover('ipad',self.fp,authorize=self.auth,pairing_intent=True)

    def test_ordinary_authenticated_companion_needs_plugin_remote_approval(self):
        result=self.submit();self.assertEqual(result['status'],'awaiting_local_approval');self.assertFalse(result['paired']);self.assertEqual(self.fork.approvals,0)
        pending=self.bridge.pending_local(local_authorize=self.local)['requests'][0]
        result=self.bridge.approve_local(pending['attempt_id'],request_id=self.rid,client_cert_sha256=self.fp,local_authorize=self.local)
        self.assertEqual(result['status'],'awaiting_client_proof');self.assertFalse(result['paired']);self.assertEqual(self.fork.approvals,1)
    def test_an_allowed_device_submits_its_exact_pin_once_and_never_again(self):
        self.grant();first=self.submit();second=self.submit()
        self.assertEqual(first['attempt_id'],second['attempt_id']);self.assertEqual(self.fork.approvals,1)
        self.assertEqual(first['status'],'awaiting_client_proof');self.assertFalse(self.bridge.media_authorized('ipad',self.fp))
        self.assertEqual(self.state()['bindings'],{})
        self.assertFalse(self.bridge._pins)
        self.assertNotIn(self.pin,self.store.path.read_text());self.assertNotIn('"pin"',self.store.path.read_text())
        self.assertNotIn(self.pin,json.dumps(first));self.assertNotIn(self.pin,json.dumps(self.fork.calls))
    def test_paired_only_after_fork_final_proof_then_association_survives_restart(self):
        self.grant();result=self.submit();self.fork.rows[self.rid]['status']='paired'
        final=self.bridge.status('ipad',result['attempt_id'],authorize=self.auth)
        self.assertTrue(final['paired']);self.assertTrue(self.bridge.media_authorized('ipad',self.fp))
        restarted=MediaPairingBridge(self.fork,MediaPairingStore(self.store.path),clock=self.clock,monotonic=self.clock)
        self.assertTrue(restarted.media_authorized('ipad',self.fp));self.assertFalse(restarted.media_authorized('stranger',self.fp))
    def test_claim_racing_remote_grant_finishes_without_second_plugin_approval(self):
        first=self.submit();self.assertEqual(first['status'],'awaiting_local_approval')
        self.grant()
        resumed=self.bridge.status('ipad',first['attempt_id'],authorize=self.auth)
        self.assertEqual(resumed['status'],'awaiting_client_proof');self.assertEqual(self.fork.approvals,1)

    def test_remote_grant_before_claim_does_not_bypass_companion_auth(self):
        self.grant();self.valid=False
        with self.assertRaises(MediaPairingError):self.submit()
        self.assertEqual(self.fork.approvals,0);self.assertFalse(self.bridge._pins)
        self.valid=True;result=self.submit();self.assertEqual(result['status'],'awaiting_client_proof')
    def test_concurrent_duplicate_submission_spends_pin_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.grant()
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.submit(),range(8)))
        self.assertEqual(len({item['attempt_id'] for item in results}),1)
        self.assertEqual(self.fork.approvals,1)
    def test_close_wipes_owned_pin_buffer_without_remote_action_or_trust_change(self):
        result=self.submit();buffer=self.bridge._pins[result['attempt_id']][0];calls=len(self.fork.calls)
        self.bridge.close()
        self.assertEqual(buffer,bytearray(4));self.assertFalse(self.bridge._pins);self.assertFalse(self.bridge._authorizers)
        self.assertEqual(len(self.fork.calls),calls);self.assertEqual(self.state()['attempts'][result['attempt_id']]['status'],'awaiting_local_approval')

    def test_missing_explicit_permission_never_allows_a_device(self):
        with self.assertRaises(MediaPairingError):
            self.bridge.grant_remote('ipad',remote_allowed=False,source_request_id='pair_'+'1'*32,local_authorize=self.local)
        self.assertFalse(self.state()['devices'])
    def test_a_second_certificate_for_the_same_allowed_device_needs_no_new_approval(self):
        # Permission is per device, not per certificate: the device is already
        # holding a credential this host issued, and its media trust follows it.
        self.grant();self.submit();otherfp='b'*64;other=self.fork.add(otherfp)
        result=self.submit(rid=other,fp=otherfp)
        self.assertEqual(result['status'],'awaiting_client_proof');self.assertEqual(self.fork.approvals,2)
    def test_a_certificate_already_bound_to_another_device_is_refused(self):
        self.grant();self.submit();self.fork.rows[self.rid]['status']='paired'
        self.bridge.status('ipad',self.state()['attempts'][next(iter(self.state()['attempts']))]['attempt_id'],authorize=self.auth)
        self.grant('other')
        with self.assertRaises(MediaPairingError):self.bridge.submit('other',self.payload(),authorize=self.auth)
    def test_other_device_or_wrong_binding_cannot_approve_cancel_or_steal(self):
        self.grant();result=self.submit()
        with self.assertRaises(MediaPairingError):self.bridge.submit('other',self.payload(),authorize=self.auth)
        with self.assertRaises(MediaPairingError):self.bridge.status('other',result['attempt_id'],authorize=self.auth)
        with self.assertRaises(MediaPairingError):self.bridge.cancel('other',result['attempt_id'],authorize=self.auth)
        with self.assertRaises(MediaPairingError):self.bridge.approve_local(result['attempt_id'],request_id=self.rid,client_cert_sha256='b'*64,local_authorize=self.local)
        self.assertEqual(self.fork.approvals,1)
    def test_cancelled_or_expired_attempt_never_accepts_late_paired_status(self):
        self.grant();result=self.submit();cancelled=self.bridge.cancel('ipad',result['attempt_id'],authorize=self.auth)
        self.assertEqual(cancelled['status'],'cancelled');self.fork.rows[self.rid]['status']='paired'
        final=self.bridge.status('ipad',result['attempt_id'],authorize=self.auth)
        self.assertFalse(final['paired']);self.assertFalse(self.bridge.media_authorized('ipad',self.fp));self.assertFalse(self.state()['bindings'])
        otherfp='b'*64;rid=self.fork.add(otherfp);pending=self.submit(rid=rid,fp=otherfp)
        self.clock.value+=121;self.bridge.maintenance()
        self.assertEqual(self.state()['attempts'][pending['attempt_id']]['status'],'expired');self.assertFalse(self.bridge._pins)
    def test_unknown_cancel_then_late_crypto_result_is_pending_revocation_not_paired(self):
        self.grant();result=self.submit();self.fork.fail_cancel=True
        cancelled=self.bridge.cancel('ipad',result['attempt_id'],authorize=self.auth)
        self.assertEqual(cancelled['status'],'cancellation_pending')
        self.fork.rows[self.rid]['status']='paired';self.bridge.maintenance()
        record=self.state()['attempts'][result['attempt_id']]
        self.assertEqual(record['status'],'revocation_pending');self.assertFalse(self.bridge.media_authorized('ipad',self.fp))
    def test_revoke_against_a_fork_without_revocation_stays_explicitly_pending(self):
        # The pre-PLUG-4 behaviour, kept: a fork that answers `unknown_operation`
        # must never be reported as having revoked anything.
        self.grant();one=self.submit();self.fork.rows[self.rid]['status']='paired';self.bridge.status('ipad',one['attempt_id'],authorize=self.auth)
        fp2='b'*64;rid2=self.fork.add(fp2);self.grant('other');two=self.submit('other',rid2,fp2)
        self.fork.rows[rid2]['status']='paired';self.bridge.status('other',two['attempt_id'],authorize=self.auth)
        receipt=self.bridge.revoke_device('ipad',local_authorize=self.local)
        self.assertFalse(receipt['media_authorized']);self.assertFalse(receipt['media_revocation_complete']);self.assertEqual(receipt['pending_media_revocations'],1)
        self.assertFalse(receipt['certificate_revocation_supported'])
        self.assertFalse(self.bridge.media_authorized('ipad',self.fp));self.assertTrue(self.bridge.media_authorized('other',fp2))
        self.assertEqual(self.state()['bindings'][self.fp]['status'],'revocation_pending')
        self.assertEqual(self.state()['attempts'][one['attempt_id']]['status'],'revocation_pending')
        pending=self.bridge.pending_local(local_authorize=self.local)
        self.assertEqual([row['attempt_id'] for row in pending['history']],[one['attempt_id']])
        self.assertFalse(pending['certificate_revocation_supported'])
    def _paired(self,device='ipad',rid=None,fp=None):
        """One device carried all the way to a real `paired` binding."""
        self.grant(device);attempt=self.submit(device,rid,fp)
        self.fork.rows[rid or self.rid]['status']='paired'
        self.bridge.status(device,attempt['attempt_id'],authorize=self.auth)
        return attempt
    def test_revoke_against_a_capable_fork_removes_the_certificate_and_the_record(self):
        """PLUG-4 §2.1/§2.3: a finished revoke leaves nothing behind."""
        self.fork.revocation=True
        one=self._paired()
        fp2='b'*64;rid2=self.fork.add(fp2);two=self._paired('other',rid2,fp2)
        self.assertEqual(self.fork.certs,{self.fp,fp2})
        receipt=self.bridge.revoke_device('ipad',local_authorize=self.local)
        self.assertTrue(receipt['certificate_revocation_supported'])
        self.assertEqual(receipt['pending_media_revocations'],0)
        self.assertTrue(receipt['media_revocation_complete'])
        # A device that never had a grant on record has nothing unresolved
        # either, once the fork itself is the thing answering.
        self.assertTrue(self.bridge.revoke_device('never-seen',local_authorize=self.local)['media_revocation_complete'])
        # Gone in the fork, gone here, and the other device is untouched.
        self.assertEqual(self.fork.certs,{fp2})
        self.assertEqual([op for op,_ in self.fork.calls if op=='pairing.revoke'],['pairing.revoke'])
        self.assertNotIn(self.fp,self.state()['bindings'])
        self.assertNotIn(one['attempt_id'],self.state()['attempts'])
        self.assertIn(two['attempt_id'],self.state()['attempts'])
        self.assertTrue(self.bridge.media_authorized('other',fp2))
        pending=self.bridge.pending_local(local_authorize=self.local)
        self.assertEqual(pending['history'],[])
        self.assertTrue(pending['certificate_revocation_supported'])
    def test_a_revoke_the_fork_cannot_complete_is_still_pending_not_claimed(self):
        self.fork.revocation=True;self._paired();self.fork.fail_revoke=True
        receipt=self.bridge.revoke_device('ipad',local_authorize=self.local)
        self.assertEqual(receipt['pending_media_revocations'],1)
        self.assertEqual(self.state()['bindings'][self.fp]['status'],'revocation_pending')
        self.assertIn(self.fp,self.fork.certs)
        # And it is retried, not forgotten, as soon as the fork answers again.
        self.fork.fail_revoke=False
        history=self.bridge.pending_local(local_authorize=self.local)['history']
        self.assertEqual(history,[]);self.assertNotIn(self.fp,self.fork.certs)
        self.assertFalse(self.state()['bindings']);self.assertFalse(self.state()['attempts'])
    def test_leftover_revocation_records_are_caught_up_by_maintenance(self):
        """The 12 records PLUG-4 found on the host: revoked against a fork that
        could not revoke, so both the record and the certificate survived."""
        one=self._paired()
        self.bridge.revoke_device('ipad',local_authorize=self.local)
        self.assertEqual(self.state()['attempts'][one['attempt_id']]['status'],'revocation_pending')
        self.assertIn(self.fp,self.fork.certs)
        self.fork.revocation=True
        self.bridge.maintenance()
        self.assertNotIn(self.fp,self.fork.certs)
        self.assertFalse(self.state()['bindings']);self.assertFalse(self.state()['attempts'])
    def test_purge_keeps_what_is_unresolved_and_refuses_an_authorized_device(self):
        self.fork.revocation=True;self.fork.fail_revoke=True
        one=self._paired()
        with self.assertRaises(MediaPairingError):self.bridge.purge_device('ipad',local_authorize=self.local)
        self.bridge.revoke_device('ipad',local_authorize=self.local)
        held=self.bridge.purge_device('ipad',local_authorize=self.local)
        self.assertFalse(held['purged']);self.assertEqual(held['pending_media_revocations'],1)
        self.assertIn('ipad',self.state()['devices'])
        self.assertIn(one['attempt_id'],self.state()['attempts'])
        self.fork.fail_revoke=False
        done=self.bridge.purge_device('ipad',local_authorize=self.local)
        self.assertTrue(done['purged']);self.assertEqual(done['removed_bindings'],1)
        self.assertEqual(done['removed_attempts'],1)
        self.assertFalse(self.state()['devices']);self.assertFalse(self.state()['bindings'])
        self.assertFalse(self.state()['attempts'])
    def test_certificates_names_what_the_fork_holds_and_what_this_host_knows(self):
        """PLUG-4 follow-up: the three leftovers, as a command.

        They were certificates the fork authorized and core had no binding for,
        so no `devices revoke` could ever name them - and each one was still
        good for a direct stream.
        """
        self.fork.revocation=True
        self._paired()
        # Older than managed pairing: authorized in the fork, unknown here.
        self.fork.certs.update({'d'*64,'e'*64});self.fork.names['d'*64]='ipad-sim'
        self.fork.unreadable=1
        listed=self.bridge.certificates(local_authorize=self.local)
        self.assertEqual({row['client_cert_sha256'] for row in listed['certificates']},
                         {self.fp,'d'*64,'e'*64})
        by_fp={row['client_cert_sha256']:row for row in listed['certificates']}
        self.assertTrue(by_fp[self.fp]['known']);self.assertEqual(by_fp[self.fp]['device_id'],'ipad')
        self.assertFalse(by_fp['d'*64]['known']);self.assertEqual(by_fp['d'*64]['device_id'],'')
        self.assertEqual(by_fp['d'*64]['name'],'ipad-sim')
        self.assertEqual(by_fp['e'*64]['name'],'')
        self.assertEqual(listed['unknown'],2)
        self.assertEqual(listed['revoked'],[])
        # A record the fork cannot parse has no fingerprint to revoke by, so it
        # is reported rather than counted as clean.
        self.assertEqual(listed['unreadable'],1)
        self.assertFalse(listed['truncated'])
        self.assertTrue(listed['certificate_revocation_supported'])
        # Listing alone never revokes anything.
        self.assertEqual(self.fork.certs,{self.fp,'d'*64,'e'*64})
    def test_purge_unknown_never_touches_a_certificate_this_host_knows(self):
        self.fork.revocation=True
        one=self._paired()
        self.fork.certs.update({'d'*64,'e'*64})
        purged=self.bridge.certificates(local_authorize=self.local,purge_unknown=True)
        self.assertEqual(sorted(purged['revoked']),['d'*64,'e'*64])
        # The paired device keeps its certificate, its binding and its grant.
        self.assertEqual(self.fork.certs,{self.fp})
        self.assertEqual(self.state()['bindings'][self.fp]['status'],'paired')
        self.assertTrue(self.bridge.media_authorized('ipad',self.fp))
        self.assertIn(one['attempt_id'],self.state()['attempts'])
        again=self.bridge.certificates(local_authorize=self.local,purge_unknown=True)
        self.assertEqual(again['unknown'],0);self.assertEqual(again['revoked'],[])
    def test_purge_unknown_against_a_fork_that_cannot_revoke_claims_nothing(self):
        self.fork.certs.add('d'*64)
        result=self.bridge.certificates(local_authorize=self.local,purge_unknown=True)
        self.assertEqual(result['unknown'],1)
        self.assertEqual(result['revoked'],[])
        self.assertFalse(result['certificate_revocation_supported'])
        self.assertFalse(result['certificates'][0]['revoked'])
        self.assertEqual(self.fork.certs,{'d'*64})
    def test_certificates_requires_local_authority_and_a_real_flag(self):
        with self.assertRaises(MediaPairingError):self.bridge.certificates(local_authorize=None)
        with self.assertRaises(MediaPairingError):
            self.bridge.certificates(local_authorize=self.local,purge_unknown='yes')
    def test_revoke_is_addressed_by_a_lowercase_hex_certificate_digest_only(self):
        self.fork.revocation=True
        # `not_found` is a completed revocation: the fork does not authorize it.
        self.assertTrue(self.bridge._revoke_certificate('c'*64))
        # Anything that is not the digest never reaches the fork at all.
        self.assertFalse(self.bridge._revoke_certificate('C'*64))
        self.assertFalse(self.bridge._revoke_certificate('a'*63))
        self.assertFalse(self.bridge._revoke_certificate(''))
        self.assertFalse(self.bridge._revoke_certificate(None))
        self.assertEqual([fields for op,fields in self.fork.calls if op=='pairing.revoke'],
                         [{'client_cert_sha256':'c'*64}])
    def test_revoked_authorization_during_approve_does_not_associate(self):
        self.grant();self.fork.after_approve=lambda:setattr(self,'valid',False)
        result=self.submit();self.assertIn(result['status'],{'cancelled','cancellation_pending','revocation_pending'})
        self.assertFalse(self.state()['bindings']);self.assertFalse(self.bridge._pins)
    def test_restart_loses_pin_and_does_not_resend_approved_request(self):
        pending=self.submit();restarted=MediaPairingBridge(self.fork,self.store,clock=self.clock,monotonic=self.clock)
        with self.assertRaises(MediaPairingError):restarted.approve_local(pending['attempt_id'],request_id=self.rid,client_cert_sha256=self.fp,local_authorize=self.local)
        self.assertEqual(self.fork.approvals,0)
        resumed=restarted.submit('ipad',self.payload(),authorize=self.auth)
        restarted.approve_local(resumed['attempt_id'],request_id=self.rid,client_cert_sha256=self.fp,local_authorize=self.local)
        self.assertEqual(self.fork.approvals,1)
        again=MediaPairingBridge(self.fork,self.store,clock=self.clock,monotonic=self.clock)
        again.submit('ipad',self.payload(),authorize=self.auth);self.assertEqual(self.fork.approvals,1)
    def test_a_spec_b2_grant_file_carries_its_deny_forward_and_drops_the_epochs(self):
        self.store.path.write_text(json.dumps({'version':1,'grants':{
            'denied':{'epoch':3,'authorized':False,'certificates':[],'source':'companion_remote_approval',
                      'source_request_id':'pair_'+'c'*32,'updated_at':1.0},
            'ipad':{'epoch':1,'authorized':True,'certificates':[self.fp],'source':'plugin_remote_approval',
                    'source_request_id':'','updated_at':2.0}},
            'attempts':{'11111111-1111-1111-1111-111111111111':{'attempt_id':'x'}},
            'bindings':{self.fp:{'device_id':'ipad','client_cert_sha256':self.fp,'request_id':self.rid,
                                 'attempt_id':'11111111-1111-1111-1111-111111111111',
                                 'paired_at':2.0,'status':'paired'}}}))
        self.store.path.chmod(0o600)
        self.assertTrue(self.bridge.remote_denied('denied'))
        self.assertFalse(self.bridge.remote_denied('ipad'))
        # The paired certificate is what Remote needs after a restart.
        self.assertEqual(self.bridge.paired_certificate('ipad'),self.fp)
        state=self.state()
        self.assertEqual(state['version'],2);self.assertEqual(state['attempts'],{})
        self.assertNotIn('epoch',json.dumps(state))
    def test_pin_response_and_payload_shape_are_bounded(self):
        for patch in [{'pin':'12345'},{'pin':1234},{'request_id':'bad'},{'client_cert_sha256':'A'*64},{'extra':True}]:
            with self.assertRaises(MediaPairingError):self.bridge.submit('ipad',self.payload()|patch,authorize=self.auth)
        self.assertFalse(self.fork.approvals)


class PrivateSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='media-pair-');self.root=Path(self.tmp.name);self.root.chmod(0o700)
        self.path=self.root/'pair.sock';self.fp='a'*64;self.rid=str(uuid.uuid4());self.received=[]
    def tearDown(self):self.tmp.cleanup()
    def exchange(self,response,request='pairing.pending',fields=None,peer_check=lambda client:None):
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(self.path));self.path.chmod(0o600);server.listen()
        def serve():
            with server:
                with server.accept()[0] as peer:
                    data=peer.recv(4096)
                    # Keep op/key set only so fixture collection contains no PIN.
                    if data:
                        value=json.loads(data);self.received.append((value['op'],set(value)))
                        peer.sendall(response)
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        try:
            client=SunshinePairingIPC(self.path,peer_check=peer_check)
            return client.request(request,**({'request_id':self.rid,'client_cert_sha256':self.fp} if fields is None else fields))
        finally:
            thread.join(2);self.path.unlink(missing_ok=True)
    def test_real_private_unix_jsonl_bound_exchange(self):
        data={'ok':True,'request':{'request_id':self.rid,'client_cert_sha256':self.fp,'status':'pending','expires_in_ms':119000}}
        result=self.exchange((json.dumps(data)+'\n').encode())
        self.assertEqual(result['request']['status'],'pending');self.assertEqual(self.received[0][0],'pairing.pending')
    def test_approve_ack_has_no_paired_semantics_and_pin_not_retained_in_fixture(self):
        data={'ok':True,'accepted':True,'request_id':self.rid,'status':'awaiting_client_proof'}
        result=self.exchange((json.dumps(data)+'\n').encode(),'pairing.approve',{'request_id':self.rid,'client_cert_sha256':self.fp,'pin':'4826','name':'Fixture'})
        self.assertEqual(result['status'],'awaiting_client_proof');self.assertNotIn('pin',result)
    def test_duplicate_json_wrong_fingerprint_and_multi_frame_are_rejected(self):
        responses=[b'{"ok":true,"ok":false}\n',b'{"ok":true}\n{}\n',
            (json.dumps({'ok':True,'request':{'request_id':self.rid,'client_cert_sha256':'b'*64,'status':'paired','expires_in_ms':100}})+'\n').encode()]
        for response in responses:
            with self.assertRaises(MediaPairingError):self.exchange(response)
    def test_unreviewed_operation_never_connects(self):
        client=SunshinePairingIPC(self.path)
        with self.assertRaises(MediaPairingError):client.request('pairing.forget',client_cert_sha256=self.fp)
        # A reviewed operation with the wrong field set is just as unreviewed.
        with self.assertRaises(MediaPairingError):client.request('pairing.revoke',request_id=self.rid,client_cert_sha256=self.fp)
        with self.assertRaises(MediaPairingError):client.request('pairing.revoke',client_cert_sha256='A'*64)
    def test_revoke_sends_only_the_certificate_and_reads_only_a_real_status(self):
        data={'ok':True,'status':'revoked'}
        result=self.exchange((json.dumps(data)+'\n').encode(),request='pairing.revoke',
                             fields={'client_cert_sha256':self.fp})
        self.assertEqual(result,{'status':'revoked'})
        self.assertEqual(self.received[0],('pairing.revoke',{'op','client_cert_sha256'}))
        with self.assertRaises(MediaPairingError):
            self.exchange((json.dumps({'ok':True,'status':'maybe'})+'\n').encode(),request='pairing.revoke',
                          fields={'client_cert_sha256':self.fp})
    def test_the_client_store_listing_is_bounded_and_strictly_shaped(self):
        row={'client_cert_sha256':self.fp,'name':'ipad-sim','enabled':True}
        listed=self.exchange((json.dumps({'ok':True,'clients':[row],'unreadable':1,'truncated':False})+'\n').encode(),
                             request='pairing.clients',fields={})
        self.assertEqual(listed['clients'],[row])
        self.assertEqual(listed['unreadable'],1)
        self.assertEqual(self.received[0],('pairing.clients',{'op'}))
        for bad in ({'ok':True,'clients':[row]},
                    {'ok':True,'clients':[{**row,'client_cert_sha256':'A'*64}],'unreadable':0,'truncated':False},
                    {'ok':True,'clients':[{**row,'name':'x'*81}],'unreadable':0,'truncated':False},
                    {'ok':True,'clients':[{**row,'enabled':'yes'}],'unreadable':0,'truncated':False},
                    {'ok':True,'clients':[{**row,'cert':'-----BEGIN'}],'unreadable':0,'truncated':False},
                    {'ok':True,'clients':[row,row],'unreadable':0,'truncated':False}):
            with self.assertRaises(MediaPairingError):
                self.exchange((json.dumps(bad)+'\n').encode(),request='pairing.clients',fields={})
    def test_the_capability_flag_is_read_off_pairing_list_and_defaults_to_no(self):
        listed=self.exchange((json.dumps({'ok':True,'requests':[]})+'\n').encode(),request='pairing.list',fields={})
        self.assertFalse(listed['certificate_revocation_supported'])
        listed=self.exchange((json.dumps({'ok':True,'requests':[],'certificate_revocation_supported':True})+'\n').encode(),
                             request='pairing.list',fields={})
        self.assertTrue(listed['certificate_revocation_supported'])
    def test_peer_rejected_before_pin_is_sent(self):
        def reject(peer):raise MediaPairingError('media_pairing_peer_mismatch',503)
        response=b'{"ok":true}\n'
        with self.assertRaises(MediaPairingError):
            self.exchange(response,'pairing.approve',{'request_id':self.rid,'client_cert_sha256':self.fp,'pin':'4826','name':'Fixture'},peer_check=reject)
        self.assertEqual(self.received,[])
    def test_unsafe_socket_mode_is_rejected_without_connecting(self):
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(self.path));server.listen();self.path.chmod(0o666)
        try:
            with self.assertRaisesRegex(MediaPairingError,'unsafe'):
                SunshinePairingIPC(self.path,peer_check=lambda client:None).request('pairing.pending',request_id=self.rid,client_cert_sha256=self.fp)
        finally:server.close();self.path.unlink()

    def test_private_state_rejects_unsafe_permissions_and_hidden_pin_field(self):
        path=self.root/'private-state'/'state.json';store=MediaPairingStore(path)
        with store.transaction() as state:store.commit(state)
        path.chmod(0o644)
        with self.assertRaises(MediaPairingError):
            with store.transaction():pass
        for raw in ('{"version":2,"devices":{},"attempts":{},"bindings":{},"pin":"0000"}',
                    '{"version":3,"devices":{},"attempts":{},"bindings":{}}',
                    '{"version":2,"devices":{"ipad":{"pin":"0000"}},"attempts":{},"bindings":{}}'):
            path.chmod(0o600);path.write_text(raw)
            with self.assertRaises(MediaPairingError):
                with store.transaction():pass


if __name__=='__main__':unittest.main()
