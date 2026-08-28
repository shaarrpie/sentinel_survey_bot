import os

root = r'C:\Users\tiajungba\.gemini\antigravity-ide\scratch\sentinel_survey_bot'
paste_path = os.path.join(root, 'full_project_paste.txt')

# Files to append
files_to_append = [
    'survey-test.html',
    'omniroute_docs.md',
]

with open(paste_path, 'a', encoding='utf-8') as out:
    for filename in files_to_append:
        filepath = os.path.join(root, filename)
        if os.path.exists(filepath):
            out.write(f'\n=== FILE: {filename} ===\n')
            out.write(f'=== SIZE: {os.path.getsize(filepath)} bytes ===\n')
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                out.write(content)
                if not content.endswith('\n'):
                    out.write('\n')
                print(f'Appended: {filename} ({os.path.getsize(filepath)} bytes)')
            except Exception as e:
                out.write(f'[ERROR READING FILE: {e}]\n')
                print(f'Error reading {filename}: {e}')
        else:
            print(f'File not found: {filename}')

print('\nDone appending files.')
