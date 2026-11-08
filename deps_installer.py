"""依赖.exe - installs the missing drivers for PS5声音触感 (runs elevated, UAC manifest)."""
import sys
import deps


def main():
    print('PS5 手柄 · 声音触感  依赖安装\n')
    if not deps.is_admin():
        print('需要管理员权限，正在请求…')
        deps.run_elevated(sys.executable, ' '.join(sys.argv[1:]))
        return
    todo = deps.missing()
    for key, disp, ok, need in deps.status():
        print(f'  {"✓" if ok else "✗"} {disp}  （{need}需要）')
    print()
    if not todo:
        print('所有依赖都已安装，不需要做任何事。')
    else:
        for key, disp, _ok, _need in todo:
            deps.install(key)
        print()
        left = deps.missing()
        if left:
            print('还有没装好的：' + '、'.join(d[1] for d in left))
        else:
            print('全部安装完成。HidHide / usbip-win2 刚装上时建议重启一次电脑，然后再打开 PS5声音触感。')
    if '--auto' not in sys.argv:
        input('\n按回车键关闭…')


if __name__ == '__main__':
    main()
