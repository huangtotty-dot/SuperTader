import re

with open('e:/06_T/t_trader_v1.10.py', 'r', encoding='utf-8') as f:
    content = f.read()

lines = content.split('\n')

# Find PreOpenContext class definition
start_idx = None
for i, line in enumerate(lines):
    if 'class PreOpenContext' in line:
        start_idx = i
        break

# Find where preopen functions end (looking for a clear boundary)
# The preopen functions likely go from PreOpenContext to around _build_preopen_monitor_elements
# Let's find the end by looking for a significant gap or different function
end_idx = None
for i in range(start_idx + 1, len(lines)):
    if i > 5050 and lines[i].strip() and not lines[i].startswith(' ') and not lines[i].startswith('#') and not lines[i].startswith('def ') and not lines[i].startswith('class '):
        # Found a non-indented line that's not a function/class definition
        # This might be the end of the preopen section
        continue
    # Let's look for a clear boundary after _build_preopen_monitor_elements

# Actually, let's find all function definitions from PreOpenContext onwards
end_idx = len(lines)
for i in range(start_idx + 1, len(lines)):
    if i > 5050 and lines[i].strip() and not lines[i].startswith(' ') and not lines[i].startswith('#') and not lines[i].startswith('def ') and not lines[i].startswith('class ') and not lines[i].startswith('from ') and not lines[i].startswith('import '):
        # Check if this is a module-level statement (not indented, not a def/class/import)
        if not lines[i].startswith(' ') and lines[i].strip():
            # This might be a module boundary
            if i > 5050:  # After _build_preopen_monitor_elements
                end_idx = i
                break

print(f"Start: {start_idx}, End: {end_idx}")
print(f"First few lines: {lines[start_idx:start_idx+5]}")
print(f"Last few lines: {lines[end_idx-5:end_idx]}")
