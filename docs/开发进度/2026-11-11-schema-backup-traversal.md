# PR1466备份目录权限补修

目标：服务身份可穿过本次root备份目录访问私有schema，行为不依赖umask。
分支feat/gateway-schema-authority，补修基础8a453cc0；同PR继续交付，尚未上线。

已完成：B保留root owner，服务组0710；schema服务owner0700；
umask027/077真实片段检查先红后绿，部署套件33项通过，Ruff与shell语法检查通过。
schema源码未变，不扩大备份祖先或源码备份权限。

[验收记录](../validation/2026-11-11-schema-backup-traversal.md)区分命令/权限位检查与跨账号访问。
后者待交付会话实测；Devin回复、线程关闭和一次复审由父会话负责。
未完成业务验收；未重复本地全量，最终PR CI仍是确定性门禁。
