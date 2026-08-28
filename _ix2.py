import io, re
s = io.open('_insights_app.js', encoding='utf-8', errors='replace').read()
print('LEN', len(s))
# branding keywords
for kw in ['WRQualtrics', 'tractive', 'Samplitude', 'Lucid', 'Cint', 'Qualtrics', 'Forgot', 'Welcome', 'version', 'Copyright', 'terms', 'privacy', 'SIGN IN', 'Sign in', 'email', 'password']:
    n = s.count(kw)
    if n:
        i = s.find(kw)
        ctx = s[max(0,i-60):i+80].replace('\n',' ') if i>=0 else ''
        print(f'{kw} :: {n} :: ...{ctx}...')