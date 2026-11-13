"""Source-only experimental driver. Never invoked by importing or by MCP.

`run` requires the separately authorized img.mcd.cn certificate and an explicit
local override flag. `recover` can only forward unmodified official configs.
"""
import argparse
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
import uuid

from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.sampling_app import SamplingApp
from ezmcd.sampling_cert import SamplingCertificate
from ezmcd.sampling_local import LOCAL_MODE, PROBE_MODE, TRANSPORT
from ezmcd.storage import Store,read_private,write_private,secure_directory
from ezmcd.transport import NoRedirects
from config_certificate import SamplingCertificate as ConfigCertificate
from transaction import CONFIG_HOST,CONFIG_PATH,MAX_BYTES,parse_config
from transport_fence import HOSTS,snapshots
from process_receipt import require_previous_children_stopped
import resources

HERE=Path(__file__).resolve().parent


def report(stage,**values):
    print(json.dumps({'stage':stage,**values},ensure_ascii=False),flush=True)


def proxy_snapshot():
    r=subprocess.run(['/usr/sbin/scutil','--proxy'],capture_output=True,text=True,check=True,timeout=5)
    return r.stdout


def require_direct(value):
    rows=dict(line.strip().split(' : ',1) for line in value.splitlines() if ' : ' in line)
    if any(rows.get(k,'0')!='0' for k in ('HTTPEnable','HTTPSEnable','SOCKSEnable',
            'ProxyAutoConfigEnable','ProxyAutoDiscoveryEnable')):
        raise EzMcdError('readonly_capture_existing_proxy_requires_review')


def require_no_pending_restoration(root):
    # Config is shared by the one official App, regardless of project alias.
    # A fresh UUID cannot discard an earlier restoration obligation.
    folder=Path(root)/'readonly-captures'
    if not folder.exists():return
    if folder.is_symlink():raise EzMcdError('readonly_capture_records_invalid')
    for directory in folder.iterdir():
        if not re.fullmatch(r'[0-9a-f]{32}',directory.name):continue
        if directory.is_symlink() or not directory.is_dir():
            raise EzMcdError('readonly_capture_records_invalid')
        path=directory/'transaction/state.json'
        if not path.exists():continue
        state=read_private(path)
        if (state.get('candidate_may_have_applied') is True
                and not (state.get('state')=='RESTORED' and state.get('restore_observed') is True)):
            raise EzMcdError('readonly_capture_previous_restoration_required')


def prepare_dns():
    # Resolve in a bounded child before local routing is enabled; no DNS writes.
    source="""import json,socket
result={}
for host in ('api.mcd.cn','img.mcd.cn'):
    rows=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM,flags=socket.AI_CANONNAME)
    result[host]={'addresses':list(dict.fromkeys(r[4][0] for r in rows)),
        'names':list(dict.fromkeys([host]+[r[3].lower().removesuffix('.') for r in rows if r[3]]))}
print(json.dumps(result))
"""
    r=subprocess.run([sys.executable,'-I','-c',source],capture_output=True,text=True,check=True,timeout=8)
    result=json.loads(r.stdout);snapshots(result);return result


def fetch_config():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirects())
    req=urllib.request.Request('https://'+CONFIG_HOST+CONFIG_PATH,
        headers={'Accept':'application/json','Cache-Control':'no-cache'},method='GET')
    with opener.open(req,timeout=15) as r:
        if r.status!=200:raise EzMcdError('readonly_capture_config_unavailable')
        raw=r.read(MAX_BYTES+1)
    parse_config(raw)
    return raw.decode('utf-8')


def close_app(app):
    app.request_close()
    end=time.monotonic()+5
    while time.monotonic()<end:
        if not app.pids() and not app.launch_pending():return True
        time.sleep(.1)
    return not app.pids() and not app.launch_pending()


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill();child.wait(timeout=5)
    return child is None or child.poll() is not None


def command(config_path,executable,api_leaf,img_leaf,ca_directory):
    return [executable,'--mode',LOCAL_MODE,'--mode',PROBE_MODE,
        '--certs','api.mcd.cn='+str(api_leaf),'--certs','img.mcd.cn='+str(img_leaf),
        '--set','confdir='+str(ca_directory),'--set','connection_strategy=lazy',
        '--set','upstream_cert=false','--set','rawtcp=false','--set','http2=false',
        '--set','flow_detail=0','--set','termlog_verbosity=error','--set','body_size_limit=256k',
        '-s',str(HERE/'addon.py')]


def probe(certificate,phase_directory,transaction_directory):
    c=http.client.HTTPSConnection('127.0.0.1',18089,
        context=ssl.create_default_context(cafile=str(certificate)),timeout=5)
    c.set_tunnel('api.mcd.cn',443)
    try:
        c.request('POST','/bff/order/orders',body=b'{"synthetic":"readonly-preflight"}',
            headers={'Content-Type':'application/json'})
        try:
            r=c.getresponse();r.read()
            if r.status!=409 or r.getheader('x-ezmcd-local-block')!='1':
                raise EzMcdError('readonly_capture_preflight_failed')
        except http.client.RemoteDisconnected:pass
    finally:c.close()
    network=read_private(phase_directory/'network.json') if (phase_directory/'network.json').exists() else {}
    if network.get('upstream_connections',0)!=0:
        raise EzMcdError('readonly_capture_preflight_reached_upstream')
    guard=read_private(phase_directory/'guard-identity.json')
    if not re.fullmatch(r'[0-9a-f]{32}',guard.get('epoch','')):
        raise EzMcdError('readonly_capture_preflight_guard_missing')
    audit=read_private(transaction_directory/('http-audit-'+guard['epoch']+'.json'))
    if (audit.get('requests_blocked')!=1 or audit.get('api_reads_forwarded')!=0
            or audit.get('config_reads_forwarded')!=0 or audit.get('financial_requests_forwarded')!=0):
        raise EzMcdError('readonly_capture_preflight_guard_failed')


def phase(store,alias,directory,name,original,dns,executable,api_leaf,img_leaf,ca_directory,stop):
    phase_dir=directory/(name+'-'+uuid.uuid4().hex)
    secure_directory(phase_dir)
    account=store.account(alias)
    cfg={'purpose':'readonly_runtime_experiment','phase':name,'seconds':120,
        'deadline':time.time()+120,'parent_pid':os.getpid(),'transport':TRANSPORT,'local_mode':LOCAL_MODE,
        'store_root':str(store.root),'alias':alias,'account_fingerprint':fingerprint(account),
        'transaction_directory':str(directory/'transaction'),'phase_directory':str(phase_dir),
        'output':str(phase_dir),'original_config':original,'dns':dns}
    write_private(phase_dir/'driver.json',cfg)
    app=SamplingApp(account['binary']);app.validate()
    if app.pids():raise EzMcdError('readonly_capture_close_app_before_phase')
    child=None;closed=False;stopped=False;errors=[];state={};seen=False;reason='phase_timed_out'
    running_before_close=None;close_signals=None
    try:
        child=subprocess.Popen(command(phase_dir/'driver.json',executable,api_leaf,img_leaf,ca_directory),
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,umask=0o077,
            env=dict(os.environ,EZMCD_READONLY_CONFIG=str(phase_dir/'driver.json'),
                PYTHONPATH=os.pathsep.join([str(HERE),str(HERE.parents[1]/'src')])) )
        ready=phase_dir/'transport-ready.json'
        end=time.monotonic()+10
        while time.monotonic()<end and not ready.exists():
            if stop[0] or child.poll() is not None:raise EzMcdError('readonly_capture_startup_stopped')
            time.sleep(.1)
        if not ready.exists() or read_private(ready)!={'transport':TRANSPORT,'local_mode':LOCAL_MODE,'system_proxy_modified':False}:
            raise EzMcdError('readonly_capture_transport_not_ready')
        probe(ca_directory/'mitmproxy-ca-cert.pem',phase_dir,directory/'transaction')
        if stop[0]:raise EzMcdError('readonly_capture_cancelled')
        report('COLLECT_READY' if name=='collect' else 'RESTORE_READY',
            instruction='请在本轮打开的麦当劳 App 进入麦钱包；不用操作购物车。',phase_directory=str(phase_dir))
        app.start()
        end=time.monotonic()+max(0,cfg['deadline']-time.time())
        while time.monotonic()<end and not stop[0]:
            if child.poll() is not None:raise EzMcdError('readonly_capture_child_stopped')
            if app.pids():seen=True
            if app.launch_failed():raise EzMcdError('readonly_capture_app_launch_failed')
            state_path=directory/'transaction/state.json'
            if state_path.exists():
                state=read_private(state_path)
                if ((name=='collect' and state['state']=='RECOVERY_REQUIRED')
                        or (name=='recover' and state['state']=='RESTORED')):
                    reason=('restoration_verified' if name=='recover' else
                        'runtime_saved' if state.get('runtime_saved') else 'recovery_required')
                    break
            if seen and not app.pids() and not app.launch_pending():
                reason='app_exited_before_phase_completion';break
            time.sleep(.1)
        if stop[0]:reason='cancelled'
    except Exception as error:
        reason='phase_error'
        errors.append(error.code if isinstance(error,EzMcdError) else type(error).__name__)
    finally:
        report('PHASE_ENDING',phase=name,reason=reason,
            instruction='本阶段已结束，工具现在主动退出麦当劳 App，再停止本轮采样。')
        try:
            running_before_close=bool(app.pids())
            closed=close_app(app)
            signalled=getattr(app,'signalled',None)
            if isinstance(signalled,set):close_signals=len(signalled)
        except Exception:errors.append('app_close_unverified')
        try:stopped=stop_child(child)
        except Exception:errors.append('child_stop_unverified')
        if not closed:errors.append('official_app_still_running')
        result={'phase':name,'official_app_closed':closed,'capture_process_stopped':stopped,
            'app_observed':seen,'completion_reason':reason,'app_running_before_tool_close':running_before_close,
            'app_close_signal_count':close_signals,'errors':sorted(set(errors)),'phase_directory':str(phase_dir)}
        write_private(phase_dir/'cleanup.json',result)
        report('PHASE_STOPPED',**result)
    return result


def run(store,alias,executable,*,recover=None,update=False):
    before=proxy_snapshot();require_direct(before)
    if not recover:require_no_pending_restoration(store.root)
    account=store.account(alias)
    directory=Path(recover).absolute() if recover else store.root/'readonly-captures'/uuid.uuid4().hex
    if recover and (directory.parent!=store.root/'readonly-captures' or directory.is_symlink()
            or not re.fullmatch(r'[0-9a-f]{32}',directory.name)):
        raise EzMcdError('readonly_capture_directory_invalid')
    if recover:
        require_previous_children_stopped(directory)
        existing=read_private(directory/'transaction/state.json')
        previous_cleanup=read_private(directory/'cleanup.json') if (directory/'cleanup.json').exists() else {}
        fully_clean=(all(previous_cleanup.get(k) is True for k in ('official_app_closed',
            'capture_processes_stopped','interphase_shutdown_verified','system_proxy_unchanged',
            'temporary_leaf_keys_removed')) and previous_cleanup.get('errors')==[]
            and resources.removed(store.root,directory))
        if existing['state']=='RESTORED' and fully_clean:
            result={'capture_directory':str(directory),'restoration_verified':True,
                'runtime_saved':existing['runtime_saved'],'account_updated':False,'already_restored':True}
            if update and existing['runtime_saved']:
                from import_evidence import apply_evidence
                result['import']=apply_evidence(store,alias,directory)
                result['account_updated']=result['import']['account_updated']
            report('FINISHED',**result);return result
    dns=prepare_dns()
    original='' if recover else fetch_config()
    stop=[False];handlers={};phases=[];errors=[];interphase=False
    for sig in (signal.SIGINT,signal.SIGTERM):
        handlers[sig]=signal.signal(sig,lambda *_:stop.__setitem__(0,True))
    secure_directory(directory)
    try:
        with ExitStack() as stack:
            stack.enter_context(store.lock(account['identity']))
            api=SamplingCertificate(store);img=ConfigCertificate(store)
            api_dir=stack.enter_context(api.lease());stack.enter_context(img.lease())
            if not recover:require_no_pending_restoration(store.root)
            resources.cleanup_previous(store.root,directory)
            api_leaf=stack.enter_context(api.server_certificate());img_leaf=stack.enter_context(img.server_certificate())
            resources.record(directory,[api_leaf,img_leaf])
            app=SamplingApp(account['binary']);app.validate()
            if not close_app(app):raise EzMcdError('readonly_capture_close_official_app_first')
            if not recover:
                result=phase(store,alias,directory,'collect',original,dns,executable,api_leaf,img_leaf,api_dir,stop)
                phases.append(result)
                if not result['official_app_closed'] or not result['capture_process_stopped']:
                    raise EzMcdError('readonly_capture_manual_recovery_required')
                interphase=True
            else:interphase=True # Recorded prior children and the current App have been checked.
            state_path=directory/'transaction/state.json'
            state=read_private(state_path) if state_path.exists() else {}
            if recover and state.get('state')=='RESTORED':
                # The former child already observed restoration, but its parent
                # may have died before closing the App/removing leaf keys. Those
                # operations have now actually been completed/owned by this run.
                phases.append({'phase':'cleanup_existing_capture','official_app_closed':True,
                    'capture_process_stopped':True,'app_observed':False,'errors':[],
                    'phase_directory':str(directory)})
            if state.get('candidate_may_have_applied') and state.get('state')!='RESTORED' and not stop[0]:
                # Fresh child/epoch after confirmed shutdown drops all candidate
                # response state. It can never invoke Transaction.prepare.
                require_previous_children_stopped(directory)
                if proxy_snapshot()!=before:raise EzMcdError('readonly_capture_proxy_changed_externally')
                phases.append(phase(store,alias,directory,'recover','',dns,executable,api_leaf,img_leaf,api_dir,stop))
    except Exception as error:
        errors.append(error.code if isinstance(error,EzMcdError) else type(error).__name__)
    finally:
        for sig,handler in handlers.items():signal.signal(sig,handler)
        try:after=proxy_snapshot()
        except (OSError,subprocess.SubprocessError):
            after=None;errors.append('proxy_state_unreadable')
        try:leaves_removed=resources.removed(store.root,directory)
        except (OSError,EzMcdError,ValueError,KeyError):
            leaves_removed=False;errors.append('temporary_leaf_cleanup_unverified')
        cleanup={'official_app_closed':bool(phases) and all(p['official_app_closed'] for p in phases),
            'capture_processes_stopped':bool(phases) and all(p['capture_process_stopped'] for p in phases),
            'interphase_shutdown_verified':interphase,'system_proxy_unchanged':before==after,
            'system_proxy_modified':False,'errors':errors+[e for p in phases for e in p['errors']],
            'temporary_leaf_keys_removed':leaves_removed,
            'phases':phases,'api_certificate_retained':True,'config_certificate_retained':True}
        if (directory/'cleanup.json').exists():
            write_private(directory/'cleanup-history'/(uuid.uuid4().hex+'.json'),read_private(directory/'cleanup.json'))
        write_private(directory/'cleanup.json',cleanup)
    result={'capture_directory':str(directory),'account_updated':False,'cleanup':cleanup}
    state_path=directory/'transaction/state.json'
    state=read_private(state_path) if state_path.exists() else {}
    result['restoration_verified']=state.get('state')=='RESTORED' and state.get('restore_observed') is True
    result['runtime_saved']=state.get('runtime_saved') is True
    result['runtime_context_matches_restore']=state.get('runtime_context_matches_restore') is True
    result['restoration_required']=bool(state.get('candidate_may_have_applied') and not result['restoration_verified'])
    if result['restoration_required']:
        result['next_action']='配置恢复尚未确认，账号未更新。确认麦当劳已退出后，用以下命令仅恢复官方配置。'
        result['recovery_command']=[sys.executable,str(Path(__file__).resolve()),'--root',str(store.root),
            '--mitmdump',executable,'recover',alias,str(directory)]+(['--update-account'] if update else [])
    if update and result['restoration_verified'] and result['runtime_saved'] and result['runtime_context_matches_restore']:
        from import_evidence import apply_evidence
        result['import']=apply_evidence(store,alias,directory)
        result['account_updated']=result['import']['account_updated']
    elif result['restoration_verified'] and result['runtime_saved'] and not result['runtime_context_matches_restore']:
        result['next_action']='官方配置恢复已确认，但两阶段设备上下文不同，本轮不更新账号。'
    report('FINISHED',**result)
    return result


def successful_result(action,update,result):
    if result.get('restoration_verified') is not True:return False
    if (action=='run' or update) and result.get('runtime_saved') is not True:return False
    if update and not (result.get('account_updated') or result.get('import',{}).get('already_imported')):
        return False
    cleanup=result.get('cleanup')
    if cleanup and (cleanup.get('errors') or any(cleanup.get(k) is not True for k in
        ('official_app_closed','capture_processes_stopped','interphase_shutdown_verified',
         'system_proxy_unchanged','temporary_leaf_keys_removed'))):return False
    return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--mitmdump',type=Path)
    sub=parser.add_subparsers(dest='action',required=True)
    for action in ('prepare-cert','install-cert','cert-status','remove-cert'):sub.add_parser(action)
    for action in ('run','recover','import'):
        p=sub.add_parser(action);p.add_argument('alias')
        if action in ('recover','import'):p.add_argument('directory',type=Path)
        if action=='run':p.add_argument('--allow-local-config-change',action='store_true',required=True)
        if action in ('run','recover'):p.add_argument('--update-account',action='store_true')
    args=parser.parse_args();store=Store(args.root)
    if args.action.endswith('cert') or args.action=='cert-status':
        method={'prepare-cert':'prepare','install-cert':'install','remove-cert':'remove','cert-status':'status'}[args.action]
        report('CERTIFICATE_RESULT',**getattr(ConfigCertificate(store),method)());return
    if args.action=='import':
        from import_evidence import apply_evidence
        report('IMPORT_RESULT',**apply_evidence(store,args.alias,args.directory));return
    if not args.mitmdump or not args.mitmdump.is_file():parser.error('An explicit mitmdump executable is required')
    result=run(store,args.alias,str(args.mitmdump.resolve()),recover=args.directory if args.action=='recover' else None,
        update=args.update_account)
    if not successful_result(args.action,args.update_account,result):raise SystemExit(2)


if __name__=='__main__':
    try:main()
    except (EzMcdError,OSError,ValueError,subprocess.SubprocessError) as error:
        report('STOPPED',error=error.code if isinstance(error,EzMcdError) else type(error).__name__)
        raise SystemExit(2)
