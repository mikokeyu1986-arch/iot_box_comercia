"""运行这个脚本在客户机上，诊断打印慢的原因"""
import subprocess, sys, time, urllib.request, ssl, json, os

print("=" * 55)
print("  IoT 打印速度诊断工具")
print("=" * 55)

# 1. Python版本
print(f"\n[1] Python版本: {sys.version}")

# 2. 网络延迟
print(f"\n[2] 网络延迟测试")
for host in ["barchina.oduo.es", "8.8.8.8"]:
    s = time.time()
    try:
        subprocess.run(["ping", "-n", "1", host], capture_output=True, text=True, timeout=5)
        print(f"  Ping {host}: {(time.time()-s)*1000:.0f}ms")
    except:
        print(f"  Ping {host}: 失败")

# 3. HTTPS确认请求耗时（这是每次打印都要做的）
print(f"\n[3] HTTPS确认请求耗时 (模拟打印后的确认)")
url = "https://barchina.oduo.es/iot/box/send_websocket"
params = {"params": {"session_id": "test", "iot_box_identifier": "test", "device_identifier": "test", "status": "success", "result": {}, "action_args": {}}}
body = json.dumps(params).encode("utf-8")
req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
ctx = ssl._create_unverified_context()
times = []
for i in range(3):
    s = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            resp.read()
        times.append((time.time()-s)*1000)
        print(f"  请求{i+1}: {times[-1]:.0f}ms")
    except Exception as e:
        print(f"  请求{i+1} 错误: {e}")
if times:
    print(f"  平均: {sum(times)/len(times):.0f}ms")

# 4. 打印机枚举
print(f"\n[4] 打印机枚举耗时")
try:
    import win32print
    s = time.time()
    printers = win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
    print(f"  win32print: {(time.time()-s)*1000:.1f}ms, 找到{len(printers)}台打印机")
except Exception as e:
    print(f"  win32print: {e}")

# 5. 关键库导入耗时
print(f"\n[5] 关键库导入耗时")
for lib in ["cryptography", "PIL", "fastapi", "websockets", "yaml"]:
    s = time.time()
    try:
        __import__(lib)
        print(f"  {lib}: {(time.time()-s)*1000:.1f}ms")
    except:
        print(f"  {lib}: 未安装")

print(f"\n完成！请把以上输出发给我。")
input("\n按 Enter 退出...")
