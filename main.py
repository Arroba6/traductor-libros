"""Arranque de la app: si algo falla al cargar, muestra el error en pantalla."""
import traceback

try:
    from app import TraductorLibrosApp
    _error = None
except Exception:
    _error = traceback.format_exc()

if _error is None:
    TraductorLibrosApp().run()
else:
    from kivy.app import App
    from kivy.uix.textinput import TextInput

    class AppError(App):
        def build(self):
            return TextInput(text="La app ha encontrado un error al arrancar.\n"
                                  "Haz una captura de pantalla y envíasela a Claude:\n\n" + _error,
                             readonly=True, font_size="13sp")

    AppError().run()
