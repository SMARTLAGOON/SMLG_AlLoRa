from lilygo_oled import OLED
import machine

class OLED_Screen:

    def __init__(self, l_32, img_data=None, layout_config=None, button=False) -> None:
        l = l_32
        self.screen = OLED(l.i2c)
        self.on = True
        self.img_data = img_data
        self.layout_config = layout_config or []
        self.status = {}
        self.previous_status = {}  # Keep track of the previous status
        self.logo_drawn = False
        self.full_refresh = True  # Flag to indicate if a full refresh is needed
        if button:
            self.button = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
            self.button.irq(trigger=machine.Pin.IRQ_FALLING, handler=self.toggle_screen)

    def toggle_screen(self, pin):
        self.on = not self.on
        if self.on:
            self.logo_drawn = False  # Ensure logo is redrawn
            self.full_refresh = True  # Ensure full refresh
            self.update_screen()
        else:
            self.empty_screen()

    def update(self, new_status):
        self.status = new_status
        if self.on:
            self.update_screen()

    def update_screen(self):
        if not self.logo_drawn and self.img_data:
            self.show_logo()
            self.logo_drawn = True

        for item in self.layout_config:
            key = item['key']
            # Handle composite fields with a custom format or directly take the value
            dynamic_value = item.get('format', lambda status: status.get(key, item.get('default', '')))(self.status)
            
            # Determine if we need to update (full refresh, value changed, or static content after full refresh)
            needs_update = (self.full_refresh or 
                            dynamic_value != self.previous_status.get(key) or 
                            (self.full_refresh and item.get('static', False)))

            if needs_update:
                self.clear_area(**item['area'])
                text = str(dynamic_value)
                self.render_text(text.strip(), **item['pos'])

        self.previous_status = self.status.copy()  # Update the previous status
        self.screen.show()
        self.full_refresh = False  # Reset the full refresh flag after updating


    def clear_area(self, x, y, w, h):
        self.screen.fill_rect(x, y, w, h, 0)

    def render_text(self, text, x, y):
        self.screen.text(text, x, y, 1)

    def show_logo(self):
        if self.img_data and not self.logo_drawn:
            for y, row in enumerate(self.img_data):
                for x, pixel in enumerate(row):
                    self.screen.pixel(x, y, pixel)

    def set_layout(self, layout_config):
        self.layout_config = layout_config

    def empty_screen(self):
        self.screen.fill(0)
        self.screen.show()
        self.logo_drawn = False
