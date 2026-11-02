"""海康 XHJ-CW-GA-HK3 信号机控制接口层（软请求）。

主要入口：``interface.hikvision.controller_link.ControllerLink``。

本接口只发「相位指示」（软请求），绝不直接下发灯色或配时；
最小绿/最大绿/黄灯/全红/安全联锁全部由信号机内部裁决。
"""
