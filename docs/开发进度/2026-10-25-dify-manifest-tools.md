# WF01发布清单收敛修复

基线a198ecc1，分支fix/dify-manifest-tools。修复一处缺省归一条件，保持Dify发布工具集合权威。
真实TOML到收敛的空工具/单工具两条反例红到绿，未绑定条目的默认行为不变。
下一步合并部署后复跑WF01清单dry-run，必须unchanged再交付清单；未执行生产权限扩展。
