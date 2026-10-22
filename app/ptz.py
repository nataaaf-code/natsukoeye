import os
import hashlib
import base64
import urllib.request
from datetime import datetime, timezone

class BardiPTZ:
    def __init__(self, ip="192.168.1.3", port=10000, user="admin", password="admin"):
        self.ip = ip
        self.port = port
        self.user = user
        self.password = password
        self.service_url = f"http://{self.ip}:{self.port}/onvif/ptz_service"

    def _make_soap(self, action_xml):
        nonce = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce).decode()
        created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        digest_input = nonce + created.encode() + self.password.encode()
        pwd_digest = base64.b64encode(hashlib.sha1(digest_input).digest()).decode()

        return f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
            xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Header>
    <wsse:Security>
      <wsse:UsernameToken>
        <wsse:Username>{self.user}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{pwd_digest}</wsse:Password>
        <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
        <wsu:Created>{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>
  </s:Header>
  <s:Body>
    {action_xml}
  </s:Body>
</s:Envelope>'''

    def move(self, direction="stop", speed=0.4, profile="PROFILE_000"):
        x, y = 0.0, 0.0
        if direction == "left":
            x = -speed
        elif direction == "right":
            x = speed
        elif direction == "up":
            y = speed
        elif direction == "down":
            y = -speed
        elif direction == "stop":
            return self.stop(profile)

        action = f'''<tptz:ContinuousMove>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:Velocity>
            <tt:PanTilt x="{x}" y="{y}"/>
          </tptz:Velocity>
        </tptz:ContinuousMove>'''
        soap = self._make_soap(action)
        try:
            req = urllib.request.Request(self.service_url, data=soap.encode(), headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
            res = urllib.request.urlopen(req, timeout=3.5)
            return True, f"Moving {direction}"
        except Exception as e:
            return False, str(e)

    def stop(self, profile="PROFILE_000"):
        action = f'''<tptz:Stop>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:PanTilt>true</tptz:PanTilt>
          <tptz:Zoom>true</tptz:Zoom>
        </tptz:Stop>'''
        soap = self._make_soap(action)
        try:
            req = urllib.request.Request(self.service_url, data=soap.encode(), headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
            res = urllib.request.urlopen(req, timeout=3.5)
            return True, "Stopped"
        except Exception as e:
            return False, str(e)
