#!/usr/bin/env python3
"""Read gitignored .env; encrypt deployment secrets without printing values."""
import json, os, pathlib, secrets, shlex, subprocess
root=pathlib.Path(__file__).resolve().parent.parent
os.chdir(root)
values={}
for line in (root/'.env').read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#'): continue
    key,sep,val=line.removeprefix('export ').partition('=')
    if not sep: raise SystemExit('Expected KEY=value in .env')
    tokens=shlex.split(val, comments=True)
    values[key.strip()]=' '.join(tokens)
required=['SPLUNK_REALM','SPLUNK_ACCESS_TOKEN','KEDIFY_ORG_ID','KEDIFY_API_KEY']
for key in required:
    if not values.get(key): raise SystemExit('Missing '+key+' in .env')
local=root/'.local';local.mkdir(mode=0o700,exist_ok=True)
keyfile=local/'sops.agekey'
if not keyfile.exists(): subprocess.run(['age-keygen','-o',str(keyfile)],check=True,capture_output=True)
keyfile.chmod(0o600)
pub=subprocess.check_output(['age-keygen','-y',str(keyfile)],text=True).strip()
password=local/'postgres-password'
if not password.exists(): password.write_text(secrets.token_urlsafe(32))
password.chmod(0o600)
(root/'.sops.yaml').write_text('creation_rules:\n  - path_regex: infrastructure/secrets/.*\\.enc\\.yaml$\n    encrypted_regex: "^(data|stringData)$"\n    age: '+pub+'\n')
def secret(name,namespace,data):return {'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':namespace},'type':'Opaque','stringData':data}
objects=[('splunk',secret('splunk-credentials','observability',{'splunk_observability_access_token':values['SPLUNK_ACCESS_TOKEN']})),('kedify',secret('kedify-agent','keda',{'apikey':values['KEDIFY_API_KEY'],'org_id':values['KEDIFY_ORG_ID']})),('postgres',secret('postgres-credentials','keda',{'password':password.read_text()})),('settings',{'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'cluster-settings','namespace':'flux-system'},'data':{'SPLUNK_REALM':values['SPLUNK_REALM']}})]
paths=[]
for name,obj in objects:
    filename=name+'.enc.yaml'; paths.append(filename)
    encrypted=subprocess.check_output(['sops','--filename-override',str(root/'infrastructure/secrets'/filename),'--encrypt','--age',pub,'--encrypted-regex','^(data|stringData)$','--input-type','json','--output-type','yaml','/dev/stdin'],input=json.dumps(obj).encode())
    (root/'infrastructure/secrets'/filename).write_bytes(encrypted)
(root/'infrastructure/secrets/kustomization.yaml').write_text('apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n'+''.join('  - '+p+'\n' for p in paths))
subprocess.run(['ruby','-ryaml','-e',"p='clusters/mobility-global/reconciliation.yaml'; docs=YAML.load_stream(File.read(p)); docs.each {|d|d['spec'].delete('suspend') if d.dig('metadata','name')=='secrets'};File.write(p,docs.map {|d|YAML.dump(d)}.join)"],check=True)
print('Encrypted secrets prepared. Commit ciphertext and .sops.yaml; keep .local and .env private.')
