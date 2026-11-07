import unittest
import inkcast

class TestCrdTouchUI(unittest.TestCase):
    def test_crd_touch_ui_elements_present_in_page(self):
        page = inkcast.PAGE
        
        # Verify top floating bar and buttons
        self.assertIn('id="tp-topbar"', page)
        self.assertIn('id="tpPrev"', page)
        self.assertIn('id="tpNext"', page)
        self.assertIn('id="tpEnter"', page)
        self.assertIn('id="tpInkBtn"', page)
        self.assertIn('id="tpUndo"', page)
        self.assertIn('class="tp-drag-handle"', page)
        self.assertIn('id="tpConfig"', page)
        self.assertIn('id="tpExit"', page)
        
        # Verify Chrome Remote Desktop modal and options
        self.assertIn('id="touch-config-modal"', page)
        self.assertIn('Configure Touch Input', page)
        self.assertIn('Trackpad mode', page)
        self.assertIn('Direct touch mode', page)
        self.assertIn('id="closeTouchConfig"', page)
        self.assertIn('id="crd-desc"', page)
        
        # Verify crisp compact mouse cursor (16px) and click ripples
        self.assertIn('id="mouse-cursor"', page)
        self.assertIn('width="16"', page)
        self.assertIn('click-ripple', page)
        self.assertIn('ripple-left', page)
        self.assertIn('ripple-right', page)
        
        # Verify stage coordinate helper
        self.assertIn('getStageRect()', page)
        
        # Verify old bulky HUD was removed
        self.assertNotIn('id="hud"', page)
        self.assertNotIn('id="cur-ring"', page)

if __name__ == '__main__':
    unittest.main()

