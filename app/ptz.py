import os
import re
import hashlib
import base64
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import logging

logger = logging.getLogger("bardi_control")

class BardiCameraControl:
    def __init__(self, ip="192.168.1.3", port=10000, user="admin", password="admin"):
        self.ip = ip
        self.port = port
        self.user = user
        self.password = password
        self.ptz_url = f"http://{self.ip}:{self.port}/onvif/ptz_service"
        self.image_url = f"http://{self.ip}:{self.port}/onvif/image_service"
        self.device_url = f"http://{self.ip}:{self.port}/onvif/device_service"

    def _make_soap(self, action_xml, extra_namespaces=""):
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
            xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema"{extra_namespaces}>
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

    def _send(self, url, action_xml, timeout=4.0):
        soap = self._make_soap(action_xml)
        req = urllib.request.Request(
            url,
            data=soap.encode('utf-8'),
            headers={'Content-Type': 'application/soap+xml; charset=utf-8'}
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read().decode('utf-8', errors='replace')

    # -------------------------------------------------------------------------
    # PTZ Controls
    # -------------------------------------------------------------------------
    def move(self, direction="stop", speed=0.4, profile="PROFILE_000"):
        x, y, z = 0.0, 0.0, 0.0
        is_zoom = False
        if direction == "left":
            x = -speed
        elif direction == "right":
            x = speed
        elif direction == "up":
            y = speed
        elif direction == "down":
            y = -speed
        elif direction in ("zoomin", "zoom_in", "in"):
            z = speed
            is_zoom = True
        elif direction in ("zoomout", "zoom_out", "out"):
            z = -speed
            is_zoom = True
        elif direction == "stop":
            return self.stop(profile)

        if is_zoom:
            action = f'''<tptz:ContinuousMove>
              <tptz:ProfileToken>{profile}</tptz:ProfileToken>
              <tptz:Velocity>
                <tt:Zoom x="{z}"/>
              </tptz:Velocity>
            </tptz:ContinuousMove>'''
        else:
            action = f'''<tptz:ContinuousMove>
              <tptz:ProfileToken>{profile}</tptz:ProfileToken>
              <tptz:Velocity>
                <tt:PanTilt x="{x}" y="{y}"/>
              </tptz:Velocity>
            </tptz:ContinuousMove>'''
        try:
            self._send(self.ptz_url, action, timeout=3.5)
            return True, f"Moving {direction}"
        except Exception as e:
            return False, str(e)

    def stop(self, profile="PROFILE_000"):
        action = f'''<tptz:Stop>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:PanTilt>true</tptz:PanTilt>
          <tptz:Zoom>true</tptz:Zoom>
        </tptz:Stop>'''
        try:
            self._send(self.ptz_url, action, timeout=3.5)
            return True, "Stopped"
        except Exception as e:
            return False, str(e)

    # -------------------------------------------------------------------------
    # PTZ Presets
    # -------------------------------------------------------------------------
    def get_presets(self, profile="PROFILE_000"):
        action = f'<tptz:GetPresets><tptz:ProfileToken>{profile}</tptz:ProfileToken></tptz:GetPresets>'
        try:
            xml_str = self._send(self.ptz_url, action, timeout=4.0)
            presets = []
            # Extract Preset tokens and names via regex / XML
            matches = re.findall(r'<tptz:Preset\s+token="([^"]+)"><tt:Name>([^<]+)</tt:Name>', xml_str)
            for token, name in matches:
                presets.append({"token": token, "name": name})
            return True, presets
        except Exception as e:
            logger.error(f"Error get_presets: {e}")
            return False, []

    def set_preset(self, preset_name, profile="PROFILE_000"):
        name_clean = re.sub(r'[^a-zA-Z0-9_\-\s]', '', preset_name).strip() or "Posisi"
        action = f'''<tptz:SetPreset>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:PresetName>{name_clean}</tptz:PresetName>
        </tptz:SetPreset>'''
        try:
            xml_str = self._send(self.ptz_url, action, timeout=4.0)
            token_match = re.search(r'<tptz:PresetToken>([^<]+)</tptz:PresetToken>', xml_str)
            token = token_match.group(1) if token_match else "PRESET_X"
            return True, {"token": token, "name": name_clean}
        except Exception as e:
            return False, str(e)

    def goto_preset(self, preset_token, profile="PROFILE_000"):
        action = f'''<tptz:GotoPreset>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:PresetToken>{preset_token}</tptz:PresetToken>
        </tptz:GotoPreset>'''
        try:
            self._send(self.ptz_url, action, timeout=4.0)
            return True, f"Moving to preset {preset_token}"
        except Exception as e:
            return False, str(e)

    def remove_preset(self, preset_token, profile="PROFILE_000"):
        action = f'''<tptz:RemovePreset>
          <tptz:ProfileToken>{profile}</tptz:ProfileToken>
          <tptz:PresetToken>{preset_token}</tptz:PresetToken>
        </tptz:RemovePreset>'''
        try:
            self._send(self.ptz_url, action, timeout=4.0)
            return True, f"Preset {preset_token} removed"
        except Exception as e:
            return False, str(e)

    # -------------------------------------------------------------------------
    # Imaging & Night Mode
    # -------------------------------------------------------------------------
    def get_imaging(self, video_source="V_SRC_000"):
        action = f'<timg:GetImagingSettings><timg:VideoSourceToken>{video_source}</timg:VideoSourceToken></timg:GetImagingSettings>'
        try:
            xml_str = self._send(self.image_url, action, timeout=4.0)
            def _find_val(tag, default):
                m = re.search(rf'<tt:{tag}>([^<]+)</tt:{tag}>', xml_str)
                return m.group(1) if m else default

            ircut = _find_val("IrCutFilter", "AUTO")
            brightness = float(_find_val("Brightness", 50.0))
            contrast = float(_find_val("Contrast", 50.0))
            saturation = float(_find_val("ColorSaturation", 50.0))
            sharpness = float(_find_val("Sharpness", 50.0))

            return True, {
                "ircut": ircut,
                "brightness": int(round(brightness)),
                "contrast": int(round(contrast)),
                "saturation": int(round(saturation)),
                "sharpness": int(round(sharpness))
            }
        except Exception as e:
            logger.error(f"Error get_imaging: {e}")
            return False, {"ircut": "AUTO", "brightness": 50, "contrast": 50, "saturation": 50, "sharpness": 50}

    def set_imaging(self, ircut=None, brightness=None, contrast=None, saturation=None, sharpness=None, video_source="V_SRC_000"):
        # Retrieve current settings first so we don't overwrite unspecified values
        ok, current = self.get_imaging(video_source)
        if ok and current:
            if ircut is None:
                ircut = current.get("ircut", "AUTO")
            if brightness is None:
                brightness = current.get("brightness", 50)
            if contrast is None:
                contrast = current.get("contrast", 50)
            if saturation is None:
                saturation = current.get("saturation", 50)
            if sharpness is None:
                sharpness = current.get("sharpness", 50)
        else:
            ircut = ircut or "AUTO"
            brightness = brightness if brightness is not None else 50
            contrast = contrast if contrast is not None else 50
            saturation = saturation if saturation is not None else 50
            sharpness = sharpness if sharpness is not None else 50

        action = f'''<timg:SetImagingSettings>
          <timg:VideoSourceToken>{video_source}</timg:VideoSourceToken>
          <timg:ImagingSettings>
            <tt:IrCutFilter>{ircut}</tt:IrCutFilter>
            <tt:Brightness>{float(brightness)}</tt:Brightness>
            <tt:Contrast>{float(contrast)}</tt:Contrast>
            <tt:ColorSaturation>{float(saturation)}</tt:ColorSaturation>
            <tt:Sharpness>{float(sharpness)}</tt:Sharpness>
          </timg:ImagingSettings>
          <timg:ForcePersistence>true</timg:ForcePersistence>
        </timg:SetImagingSettings>'''

        try:
            self._send(self.image_url, action, timeout=4.0)
            return True, {
                "ircut": ircut,
                "brightness": int(brightness),
                "contrast": int(contrast),
                "saturation": int(saturation),
                "sharpness": int(sharpness)
            }
        except Exception as e:
            logger.error(f"Error set_imaging: {e}")
            return False, str(e)

    def set_night_mode(self, mode="AUTO"):
        # Valid modes: AUTO, ON (Night Vision), OFF (Day Color)
        mode_upper = mode.upper()
        if mode_upper not in ["AUTO", "ON", "OFF"]:
            mode_upper = "AUTO"
        return self.set_imaging(ircut=mode_upper)

    # -------------------------------------------------------------------------
    # Hardware Relay & Siren
    # -------------------------------------------------------------------------
    def set_relay(self, logical_state="active", relay_token="RELAY_OUTPUT_000"):
        # State: 'active' or 'inactive'
        action = f'''<tds:SetRelayOutputState>
          <tds:RelayOutputToken>{relay_token}</tds:RelayOutputToken>
          <tds:LogicalState>{logical_state}</tds:LogicalState>
        </tds:SetRelayOutputState>'''
        try:
            self._send(self.device_url, action, timeout=4.0)
            return True, f"Relay set to {logical_state}"
        except Exception as e:
            return False, str(e)

    # -------------------------------------------------------------------------
    # System Maintenance
    # -------------------------------------------------------------------------
    def reboot(self):
        action = '<tds:SystemReboot/>'
        try:
            res = self._send(self.device_url, action, timeout=5.0)
            return True, "Kamera sedang melakukan reboot sistem."
        except Exception as e:
            return False, str(e)

# Backward compatibility alias
BardiPTZ = BardiCameraControl
