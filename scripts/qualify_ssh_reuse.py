import json, os, pathlib, pwd, resource, signal, socket, statistics, subprocess, sys, tempfile, time
from contextlib import contextmanager
import argparse
parser = argparse.ArgumentParser(description='Qualify SSH reuse using an ephemeral localhost sshd and generated test keys.')
parser.add_argument('--output', type=pathlib.Path)
parser.add_argument('--remapped-root-fixture', action='store_true', help='Disable server StrictModes only inside this isolated fixture when namespace root ownership is remapped; client host-key verification remains strict.')
args = parser.parse_args()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from raven.remote import SshBackend

root = pathlib.Path(tempfile.mkdtemp(prefix='raven-ssh-qualification-'))
os.chmod(root, 0o700)
for name in ['host', 'client']:
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root/name)], check=True)
(root/'authorized_keys').write_bytes((root/'client.pub').read_bytes())
(root/'authorized_keys').chmod(0o600)
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
config = root/'sshd_config'
config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host
PidFile {root}/pid
AuthorizedKeysFile {root}/authorized_keys
StrictModes {'no' if args.remapped_root_fixture else 'yes'}
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
AllowUsers {pwd.getpwuid(os.getuid()).pw_name}
LogLevel VERBOSE
''')
log = (root/'sshd.log').open('w+')
server = subprocess.Popen(['/usr/sbin/sshd','-D','-e','-f',str(config)], stdout=log, stderr=log)
masters=[]
try:
    for _ in range(100):
        if server.poll() is not None:
            log.flush(); print((root/'sshd.log').read_text()); raise RuntimeError('sshd stopped')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.1): pass
            break
        except OSError: time.sleep(.03)
    key=(root/'host.pub').read_text().split()
    (root/'known_hosts').write_text(f'[127.0.0.1]:{port} {key[0]} {key[1]}\n')
    opts=['-F','none','-p',str(port),'-i',str(root/'client'),'-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(root/'known_hosts'),'-o','ControlPersist=5']
    username=pwd.getpwuid(os.getuid()).pw_name
    def accepted():
        log.flush()
        return (root/'sshd.log').read_text().count('Accepted publickey')
    records=[]
    for mode in [False,True,False,True]:
        b=SshBackend('127.0.0.1',username,opts,control=mode)
        before=accepted(); cpu=resource.getrusage(resource.RUSAGE_CHILDREN); times=[]
        for _ in range(12):
            t=time.perf_counter(); output,_=b.run(['printf','qualification-ok'],timeout=5); times.append(time.perf_counter()-t)
            assert output=='qualification-ok'
        after=resource.getrusage(resource.RUSAGE_CHILDREN)
        records.append({'multiplex':mode,'commands':12,'authentications':accepted()-before,'latency_ms':times and [round(t*1000,3) for t in times], 'mean_ms':statistics.mean(times)*1000,'p50_ms':statistics.median(times)*1000,'child_cpu_seconds':(after.ru_utime+after.ru_stime)-(cpu.ru_utime+cpu.ru_stime)})
        if mode: masters.append(b)
        if mode:
            # Close only this fixture's own connection; next query must reconnect.
            command=b.argv(['true']); stop=command[:command.index(b.target)]+['-O','exit',b.target]
            subprocess.run(stop,capture_output=True,timeout=5)
    b=masters[-1]; before=accepted(); output,_=b.run(['printf','reconnected'],timeout=5)
    assert output=='reconnected' and accepted()==before+1
    # An actual remote mutation that drops its channel is sent once. Exit 255
    # is ambiguous to the client and must never cause a replay.
    marker=root/'mutation-count'
    ok,message=b.call(['sh','-c',f"printf x >> {marker}; exit 255"],timeout=5)
    assert not ok and marker.read_text()=='x'
    result={'environment':'live loopback OpenSSH; no Slurm/GPU/site claim', 'server_strict_modes':not args.remapped_root_fixture, 'client_host_key_check':'yes; exact generated host key', 'cpu_scope':'RUSAGE_CHILDREN of client driver; excludes persistent master and live server','directory':str(root),'records':records,'reconnect_authentications':accepted()-before,'mutation_executions':len(marker.read_text()),'socket_mode':oct(pathlib.Path(b.control_path).stat().st_mode & 0o777),'socket_directory_mode':oct(pathlib.Path(b.control_path).parent.stat().st_mode & 0o777)}
    (root/'result.json').write_text(json.dumps(result,indent=2))
    if args.output:
        with args.output.open('x') as output: json.dump(result, output, indent=2)
    print(json.dumps(result,indent=2))
finally:
    for b in masters:
        if b.control_path:
            try: subprocess.run(['ssh','-S',b.control_path,'-O','exit',b.target],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            except Exception: pass
    server.terminate()
    try:server.wait(timeout=5)
    except subprocess.TimeoutExpired:server.kill();server.wait()
    log.close()
    for name in ("host", "client", "authorized_keys"):
        (root/name).unlink(missing_ok=True)
