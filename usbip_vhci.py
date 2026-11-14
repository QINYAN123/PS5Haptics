"""Enumerate usbip-win2 host controllers and repair duplicate active instances."""
import ctypes as C
from ctypes import wintypes as W
import uuid

# Same interface class and PRESENT filter used by usbip-win2's libusbip.
VHCI_GUID = uuid.UUID('b4030c06-dc5f-4fcc-87eb-e5515a0935c0')
VHCI_DESCRIPTION = 'usbip-win2 检测到多个 VHCI 接口'
VHCI_ERROR = ('Multiple instances of VHCI device interface found', 'USBIP_ERROR_DEVICE_INTERFACE_LIST')
HWID = r'ROOT\USBIP_WIN2\UDE'
SERVICE = 'usbip2_ude'
CM_DRP_HARDWAREID = 2
CR_NO_SUCH_DEVNODE = 0x0D
CR_BUFFER_SMALL = 0x1A
CM_GETIDLIST_FILTER_SERVICE_PRESENT = 0x10000142  # SERVICE | PRESENT | DONOTGENERATE
CM_DISABLE_PERSIST_NO_UI = 0x0C                # PERSIST | UI_NOT_OK


def _check(result, operation):
    if result:
        raise RuntimeError(f'{operation} 失败 (CONFIGRET 0x{result:02X})')


class Vhci:
    def __init__(self):
        self.cm = C.WinDLL('cfgmgr32')
        self.guid = (C.c_ubyte * 16).from_buffer_copy(VHCI_GUID.bytes_le)

    def interfaces(self, instance_id=None):
        """Paths of active interfaces, optionally restricted to one device."""
        for _ in range(3):
            size = W.ULONG()
            _check(self.cm.CM_Get_Device_Interface_List_SizeW(
                C.byref(size), C.byref(self.guid), instance_id, 0), '查询 VHCI 接口大小')
            buffer = C.create_unicode_buffer(size.value)
            result = self.cm.CM_Get_Device_Interface_ListW(
                C.byref(self.guid), instance_id, buffer, size, 0)
            if result != CR_BUFFER_SMALL:
                _check(result, '查询 VHCI 接口')
                return {part.casefold() for part in buffer[:].split('\0') if part}
        raise RuntimeError('VHCI 接口列表正在变化，请稍后重试。')

    def controllers(self, paths):
        """Map active interfaces to exact root devnodes owned by usbip-win2."""
        size = W.ULONG()
        flags = CM_GETIDLIST_FILTER_SERVICE_PRESENT
        _check(self.cm.CM_Get_Device_ID_List_SizeW(C.byref(size), SERVICE, flags), '查询 usbip-win2 设备大小')
        buffer = C.create_unicode_buffer(size.value)
        _check(self.cm.CM_Get_Device_ID_ListW(SERVICE, buffer, size, flags), '查询 usbip-win2 设备')
        controllers = {}
        for instance_id in filter(None, buffer[:].split('\0')):
            owned = self.interfaces(instance_id) & paths
            if not owned or not instance_id.upper().startswith('ROOT\\'):
                continue
            node = self.locate(instance_id)
            ids = C.create_unicode_buffer(1024)
            length = W.ULONG(C.sizeof(ids))
            _check(self.cm.CM_Get_DevNode_Registry_PropertyW(
                node, CM_DRP_HARDWAREID, None, ids, C.byref(length), 0), '查询 usbip-win2 硬件 ID')
            if HWID in ids[:].upper().split('\0'):
                controllers[instance_id] = owned
        return controllers

    def locate(self, instance_id):
        node = W.ULONG()
        _check(self.cm.CM_Locate_DevNodeW(C.byref(node), instance_id, 0), '定位设备')
        return node

    def children(self, node):
        child = W.ULONG()
        result = self.cm.CM_Get_Child(C.byref(child), node, 0)
        while result == 0:
            yield child.value
            result = self.cm.CM_Get_Sibling(C.byref(child), child, 0)
        if result != CR_NO_SUCH_DEVNODE:
            _check(result, '查询 VHCI 子设备')

    def idle(self, instance_id):
        # An idle UDE controller exposes only its USB root hub. Any child of that hub
        # can be a device attached by another application, so leave it alone.
        children = list(self.children(self.locate(instance_id)))
        if len(children) != 1:
            return False
        hub = C.create_unicode_buffer(200)
        _check(self.cm.CM_Get_Device_IDW(children[0], hub, len(hub), 0), '查询 VHCI 根集线器')
        return hub.value.upper().startswith('USB\\ROOT_HUB30\\') and not list(self.children(children[0]))

    def disable(self, instance_id):
        _check(self.cm.CM_Disable_DevNode(self.locate(instance_id), CM_DISABLE_PERSIST_NO_UI),
               f'停用 {instance_id}')

    def enable(self, instance_id):
        _check(self.cm.CM_Enable_DevNode(self.locate(instance_id), 0), f'重新启用 {instance_id}')


def repair_duplicates(vhci, log=print):
    """Keep a controller in use, or the lowest instance ID if all are idle."""
    paths = vhci.interfaces()
    if len(paths) == 1:
        log('当前只有一个启用的 VHCI 接口，可以返回软件重试。')
        return
    if not paths:
        raise RuntimeError('没有启用的 VHCI 接口，请到「系统」检查 usbip-win2 的安装状态。')

    controllers = vhci.controllers(paths)
    owned_paths = [path for interfaces in controllers.values() for path in interfaces]
    if set(owned_paths) != paths or len(controllers) != len(paths) or len(owned_paths) != len(paths):
        raise RuntimeError('无法确认每个 VHCI 接口都属于独立的 usbip-win2 实例。请在设备管理器核对虚拟主机控制器。')
    busy = [device for device in controllers if not vhci.idle(device)]
    if len(busy) > 1:
        raise RuntimeError('多个 VHCI 实例正在使用。请先退出其他虚拟手柄或 USB/IP 软件，断开它们挂载的设备后再试。')
    keep = busy[0] if busy else min(controllers)
    disabled = []
    expected = paths.copy()
    try:
        for device in sorted(controllers.keys() - {keep}):
            if vhci.interfaces() != expected or not vhci.idle(device):
                raise RuntimeError('VHCI 设备状态已变化，请关闭其他虚拟手柄或 USB/IP 软件后再试。')
            vhci.disable(device)
            disabled.append(device)
            expected -= controllers[device]
        if vhci.interfaces() != controllers[keep]:
            raise RuntimeError('停用后 VHCI 接口仍有冲突。')
    except Exception:
        for device in reversed(disabled):
            try:
                vhci.enable(device)
            except RuntimeError as exc:
                log(f'{exc}。可在设备管理器中手动重新启用此设备。')
        raise
    log(f'已保留 {keep}，停用重复实例：' + '、'.join(disabled))
    log('已确认只剩一个启用的 VHCI 接口。返回 PS5声音触感，重新点击「启动」。')
