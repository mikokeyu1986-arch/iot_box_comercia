Set shell = CreateObject("WScript.Shell")
root = "C:\Users\Miko win\Desktop\odoo 19\instances\lang\custom_iot_box_runtime_native"
python = "C:\Users\Miko win\AppData\Local\Programs\Python\Python311\python.exe"
cmd = "cmd.exe /c cd /d """ & root & """ && set IOT_HTTP_PORT=8399 && set IOT_ESCPOS_ENCODING=cp858 && """ & python & """ run_http.py"
shell.Run cmd, 0, False
