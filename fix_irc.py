with open("watcher.py", "r") as f:
    code = f.read()

code = code.replace("import socket", "import socket\nimport ssl")
code = code.replace("IRC_PORT = 6667", "IRC_PORT = 443")

old_listen = '''    def listen_chat(self):
        sock = socket.socket()
        sock.connect((IRC_SERVER, IRC_PORT))
        sock.send(f"NICK {NICK}\\r\\n".encode("utf-8"))
        sock.send(f"JOIN #{self.channel}\\r\\n".encode("utf-8"))
        print(f"[*] Connected to #{self.channel} chat anonymously.")

        buffer = ""
        while self.running:
            try:
                data = sock.recv(2048).decode("utf-8", errors="ignore")
                if not data:
                    break
                buffer += data
                lines = buffer.split("\\r\\n")
                buffer = lines.pop()

                for line in lines:
                    if line.startswith("PING"):
                        sock.send("PONG :tmi.twitch.tv\\r\\n".encode("utf-8"))
                    elif "PRIVMSG" in line:
                        with self.lock:
                            self.msg_count += 1
            except Exception as e:
                print(f"[!] Chat connection error: {e}")
                time.sleep(5)'''

new_listen = '''    def listen_chat(self):
        context = ssl.create_default_context()
        while self.running:
            try:
                raw_sock = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=15)
                sock = context.wrap_socket(raw_sock, server_hostname=IRC_SERVER)
                sock.settimeout(60)
                sock.send(f"NICK {NICK}\\r\\n".encode("utf-8"))
                sock.send(f"JOIN #{self.channel}\\r\\n".encode("utf-8"))
                print(f"[*] Connected to #{self.channel} chat via SSL (port 443).")

                buffer = ""
                while self.running:
                    data = sock.recv(2048).decode("utf-8", errors="ignore")
                    if not data:
                        raise ConnectionError("IRC connection closed")
                    buffer += data
                    lines = buffer.split("\\r\\n")
                    buffer = lines.pop()

                    for line in lines:
                        if line.startswith("PING"):
                            sock.send("PONG :tmi.twitch.tv\\r\\n".encode("utf-8"))
                        elif "PRIVMSG" in line:
                            with self.lock:
                                self.msg_count += 1
            except Exception as e:
                print(f"[!] Chat connection error: {e} - retrying in 5s...")
                time.sleep(5)'''

assert old_listen in code, "Could not find target block in watcher.py"
code = code.replace(old_listen, new_listen)

with open("watcher.py", "w") as f:
    f.write(code)
print("Successfully updated watcher.py to use SSL port 443!")
