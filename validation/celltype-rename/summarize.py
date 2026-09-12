import json, re
from pathlib import Path
root = Path('/tmp/celltype-rename-baseline')
results = {}
for row in (root/'status').read_text().splitlines():
    repo, name, code = row.split()
    log = (root/f'{repo}.{name}.log').read_text()
    summaries = re.findall(r'^.*\b\d+ (?:passed|failed|skipped|error|errors).*$' , log, re.M)
    results[f'{repo}/tests/{name}.py'] = {'exit_code': int(code), 'summary': summaries[-1].strip('= ') if summaries else '', 'failures': re.findall(r'^FAILED .*$', log, re.M)}
log = (root/'workflow.log').read_text()
for block in re.split(r'^DONE (.+)$', log, flags=re.M)[1::2]:
    end = log.index('DONE '+block)
    start = log.rfind('\n'+block+'\n', 0, end)
    section = log[start:end]
    summaries = re.findall(r'^=+ (.+?) =+$', section, re.M)
    results['seamless-workflow/tests/'+block] = {'exit_code': 1 if re.search(r'^FAILED |^ERROR ', section, re.M) else 0, 'summary': summaries[-1] if summaries else '', 'failures': re.findall(r'^FAILED .*$', section, re.M)}
Path('/home/agent/seamless1/seamless-workflow/validation/celltype-rename/baseline.json').write_text(json.dumps(results, indent=2)+'\n')
print(len(results), 'files;', sum(bool(v['exit_code']) for v in results.values()), 'nonzero')
