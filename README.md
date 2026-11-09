# PS5 手柄 · 声音触感（PS5Haptics）

让 PC 上的 PS5 手柄（DualSense，USB 线或 [DS5Dongle](https://github.com/awalol/DS5Dongle) 系接收器，测试版）在**玩游戏、看电影、听音乐**时都用上它的全部能力：握把音圈触感、自适应扳机、手柄扬声器。

作者：[轻言black](https://space.bilibili.com/65980286)（哔哩哔哩） · GitHub：[QINYAN123/PS5Haptics](https://github.com/QINYAN123/PS5Haptics)

## 功能

- **声音转触感**：实时分析游戏 / 影片 / 音乐的声音，把打击、碰撞、爆炸、鼓点变成左右握把的音圈触感，扳机随低频冲击震动。三种风格：游戏、影院、律动。
- **原生透传**：游戏看到一只虚拟 PS5 手柄，游戏自带的自适应扳机、音圈波形、灯条都原样转到你的真手柄上，和 PS5 上的体验一致。
- **手柄扬声器**：
  - 原声：完整播放游戏声音（原味 / 低音饱满 / 人声清晰）。
  - 特殊事件：自带手柄扬声器音效的游戏（如《影之刃零》）直接播放**游戏自己发给手柄扬声器的声音**，和 PS5 一样只在关键时刻出声。
- **三个场景**：游戏 / 影院 / 音乐，各存一套设置，点卡片即时切换；设置改完实时生效。
- **适配库**：
  - 《鸣潮》：读屏识别技能状态，大招就绪时 R2 变成“咔哒”武器扳机，声骸就绪时 L2 有阻力（只读屏幕，不注入游戏）。
  - 《影之刃零》：事件 mod 驱动格挡 / 完美闪避等专属握把与扳机反馈 + 游戏原生手柄扬声器音效。
  - 通用：其他游戏按声音自动生成触感；支持 PS5 手柄的游戏走原生透传。
- **杜比全景声 / 空间音效**下也能用（按进程截取声音）。
- **一键修复手柄**：软件崩溃后手柄识别不到时点一下即可恢复（打开软件时也会自动检查）。
- **在线更新**：有新版本时软件内提示，一键下载安装。
- **四种界面语言**：中文 / English / 日本語 / 한국어。

## 下载与使用

到 Releases 下载 `PS5声音触感-版本号.zip`，解压后双击「启动PS5声音触感」。首次运行会自动检查依赖（HidHide、usbip-win2、ViGEmBus），缺什么点一下就装。详细说明见压缩包里的「使用说明.md」。

## 构建

```
python -m venv .venv
.venv\Scripts\pip install numpy scipy pillow pyaudiowpatch hidapi comtypes dxcam onnxruntime onnxocr-ppocrv5 pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm PS5Haptics.spec
.venv\Scripts\python release\build_release.py
```

## 许可

主程序 MIT（见 `LICENSE`）。虚拟手柄宿主 `viiper_host/` 为 GPL-3.0，作为独立进程运行、只通过本机套接字通信；第三方组件许可见 `licenses/`。本项目与 Sony、库洛游戏、S-GAME 均无关联。
