lines = open("extension/content.js").readlines()
depth = 0
for i, line in enumerate(lines, 1):
    for ch in line:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    if depth == 2 and 'scan' not in line.lower():
        print(f"Depth 2 at line {i}: {line.rstrip()}")
        break
