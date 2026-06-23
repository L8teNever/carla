import sys, os, datetime
path = '/'
for arg in sys.argv[1:]:
    if not arg.startswith('-'):
        path = arg
        break
if (path.startswith("'") and path.endswith("'")) or (path.startswith('"') and path.endswith('"')):
    path = path[1:-1]
if path == '/':
    win_path = 'C:/'
else:
    if path.startswith('/'):
        win_path = 'C:' + path
    else:
        win_path = path
if not os.path.exists(win_path):
    sys.exit(1)
try:
    print('total 0')
    for item in os.listdir(win_path):
        item_path = os.path.join(win_path, item)
        try:
            stat = os.stat(item_path)
            is_dir = os.path.isdir(item_path)
            perms = 'drwxr-xr-x' if is_dir else '-rw-r--r--'
            size = stat.st_size
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
            date_str = mtime.strftime('%Y-%m-%d')
            time_str = mtime.strftime('%H:%M:%S')
            print(f'{perms} 1 docker docker {size} {date_str} {time_str} {item}')
        except Exception:
            pass
except Exception:
    pass