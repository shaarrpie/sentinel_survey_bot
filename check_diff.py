import subprocess
r = subprocess.run(['git', 'diff', '--', 'extension/content.js'], capture_output=True, text=True)
print(r.stdout[:4000])
