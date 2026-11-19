========================================
  LyLaunch 插件手动安装包（模板）
========================================

本压缩包里含一个「示例插件」文件夹：demo_plugin/

【安装方法（拖入即生效）】
1. 把本压缩包解压到任意位置
2. 把整个 demo_plugin 文件夹
   （注意：是文件夹本身，不是里面的文件）
   复制 / 拖入到下面的目录：
       %LocalAppData%\LyLaunch\plugins\
   即：
       C:\Users\<你的用户名>\AppData\Local\LyLaunch\plugins\demo_plugin\
3. 重启 LyLaunch，首页「插件」区块出现「示例插件」瓦片即成功
4. 点它弹窗，表示插件已生效

【备注】
- 如果 plugins 目录不存在，手动新建即可（路径是 %LocalAppData%\LyLaunch\plugins\）
- 插件文件夹名 = 插件 Id（本例 demo_plugin），两者必须一致
- 卸载：删除 demo_plugin 文件夹即可
- 想要更多插件能力（剪贴板/通知/配置读写/调用其它插件），
  参考软件的插件开发文档 plugins/_README.md
