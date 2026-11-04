# adbx安卓真实用户验收

使用场景：验证已安装应用能否从页面进入AI助手、发问、看到新回答及清空会话。
本入口复用操作者维护的adbx，不安装新常驻服务，不重写其SSH/USB转发。
参考[Android官方ADB文档](https://developer.android.com/tools/adb)：serial选设备，shell输入操作，
UIAutomator导出控件，screencap取得设备画面。主机与账号详情从本机私有清单解析，不写进仓库。

## 执行顺序

1. 运行`~/bin/adbx --help`、`tunnel-status`、`devices -l`。多台设备必须显式选择serial，
   `offline`/`unauthorized`不继续；`shot`/`ui`只有一台时可直接用，多台通过`ANDROID_SERIAL`固定。
2. `adbx shot <私有目录>/start.jpg 1400`与`adbx ui`检查初始页面。
   默认输出的缩放截图坐标不是原生触摸坐标；按设备`wm size`换算，优先按UI bounds定位。
3. 从现装应用页面进入；若需登录，按环境手册从受控位置读取验收账号，在正常登录页输入。
   登录截图/UI含凭据，不用于公开附件。不注入会话、不清应用数据、不安装另一版冒充现装版本。
4. 把本次步骤保存为库外JSON，再运行脚本。每步有当前page、动作及预期文字。
   控件用text/content-desc/resource-id/class精确匹配，缺失或多匹配立即停止。
5. 查看`report.json`及前后截图/XML。自动PASS只证明指定画面断言；业务结论由截图与源数据核对。
   普通问答若是兜底/错误，应记业务FAIL，即便页面有文字。需要后端归因时只读访问日志，
   按设备UA和时间关联；日志无QA不等于没请求（意图识别失败发生在创建QA之前）。
6. 保留登录状态和业务记录，清本轮设备临时文件。隧道原本存在则保留；本轮新建且无人共享才
   `adbx tunnel-off`。不要杀共享ADB server；临时保活会话只清自己启动的那一个。

```bash
# 默认只校验计划，退出2（UNVERIFIED）
uv run python .claude/skills/verify-aiops-client-e2e/scripts/android_adbx.py \
  --plan <私有计划.json> --out <全新私有证据目录>
# 实际执行：设备上会产生点击/输入及正常问答请求
uv run python .claude/skills/verify-aiops-client-e2e/scripts/android_adbx.py \
  --plan <私有计划.json> --out <全新私有证据目录> --serial <选定设备> --execute
```

计划形状（示意，不含真实账号）：

```json
{"package":"example.consumer","steps":[
  {"name":"faq","page":"aiPackage/pages/chat/chat","action":"tap",
   "target":{"text":"一个推荐问题"},"expect":["预期答案中独有的片段"],
   "assistant_marker":"AI生成注意甄别","timeout":90},
  {"name":"reset","page":"aiPackage/pages/chat/chat","action":"tap",
   "target":{"text":"新对话"},"expect":["欢迎语"],"absent":["上一轮答案独有片段"],"fresh":false}
]}
```

回答验收须提供实测的`assistant_marker`，要求答案与AI标记处于同一直接父容器，排除用户气泡。
没有此字段只验证页面文字，不证明是助手回答。`expect`的多个片段须在同一非输入框节点中；`fresh`默认true，操作前任一页面已有完整匹配就拒绝（包括后台目的页，防旧答案重新出现）。
点击文本/输入本身能满足expect时拒绝，避免只验证用户回显。`absent`用于清空会话。
`next_page`用于点击导航后验证目标页；`timeout`为1–240秒（单次adbx另有25秒硬上限）。
动作仅支持tap、text、key（Back/Enter）、swipe、observe，不接受任意shell。
ASCII普通输入可使用text；中文推荐问题直接点击。UI缺少原生输入框时，先用截图确认再通过
adbx视觉操作，单列人工视觉步骤；不按后台页面的EditText盲点。无需安装额外输入法。

## 两个实测坑

- **隧道up不代表ADB能握手。** Windows通过短SSH命令启动的adb server可能随会话结束退出。
  先用宿主正常SSH核对`adb devices`；如确属会话退出，保留一个临时SSH会话运行
  `adb start-server & powershell -NoProfile -Command "Start-Sleep -Seconds 1800"`，再用adbx。
  主机别名取自当前adbx配置，核对已登记host key；不装计划任务、不改防火墙/公网端口。
- **uni-app UI树有后台页面。** 当前页面取UI树中第一个带标题的WebView，再与page核对；
  旧页面的文字/输入框不作证据。页面加载时UI与截图可能暂不同，等待稳定后再操作。
  UIAutomator在exec-out中的状态输出必须重定向，删除旧XML后再dump，失败不读缓存。

## 结果边界

0=页面断言PASS，1=已操作但行为FAIL，2=计划/设备/取证UNVERIFIED。三者不可混用。
每次使用新0700目录，报告含UTC起止、APK版本、设备标识摘要、运行器/计划摘要与截图/XML摘要。
截图是实际画面，XML可含屏幕外内容；必须一起核对，PASS不代表全文都在一屏或业务语义已认证。
不证明语音/相机/所有语言、管家APK最新版、模型稳定性或真实故障准确率。
