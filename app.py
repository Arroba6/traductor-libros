"""
Traductor de Libros — biblioteca de ebooks + traducción de libros enteros.
App Android hecha con Kivy. Se compila como APK con GitHub Actions.
"""
import json
import os
import shutil
import threading
import time
import uuid

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, DictProperty, ListProperty, ObjectProperty,
                             NumericProperty, StringProperty)
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.screenmanager import Screen, ScreenManager, SlideTransition
from kivy.utils import platform

import traductor as T

ANDROID = platform == "android"
TODAS = "Todas las estanterías"
SIN_ESTANTERIA = "Sin estantería"
TRADUCIDOS = "Traducidos"
NUEVA = "+ Nueva estantería…"


# ================================================================ Android
class Android:
    """Funciones de Android: elegir archivos, guardar en Descargas, pantalla encendida."""
    REQ = 7341

    def __init__(self):
        self._callback = None
        if not ANDROID:
            return
        from jnius import autoclass  # noqa
        from android import activity  # noqa
        self.activity = activity
        self.PythonActivity = autoclass("org.kivy.android.PythonActivity")
        self.Intent = autoclass("android.content.Intent")
        self.FileUtils = autoclass("android.os.FileUtils")
        self.FileOutputStream = autoclass("java.io.FileOutputStream")
        self.FileInputStream = autoclass("java.io.FileInputStream")
        self.ContentValues = autoclass("android.content.ContentValues")
        self.Downloads = autoclass("android.provider.MediaStore$Downloads")
        self.LayoutParams = autoclass("android.view.WindowManager$LayoutParams")

    @property
    def ctx(self):
        return self.PythonActivity.mActivity

    def elegir_libros(self, carpeta_destino, callback):
        """Abre el selector de archivos de Android. callback(lista_de_(ruta, nombre))."""
        self._callback = callback
        self._destino = carpeta_destino
        intent = self.Intent(self.Intent.ACTION_OPEN_DOCUMENT)
        intent.addCategory(self.Intent.CATEGORY_OPENABLE)
        intent.setType("*/*")
        intent.putExtra(self.Intent.EXTRA_ALLOW_MULTIPLE, True)
        self.activity.bind(on_activity_result=self._resultado)
        self.ctx.startActivityForResult(intent, self.REQ)

    def _nombre(self, uri):
        nombre = None
        try:
            cursor = self.ctx.getContentResolver().query(uri, None, None, None, None)
            if cursor is not None:
                if cursor.moveToFirst():
                    idx = cursor.getColumnIndex("_display_name")
                    if idx >= 0:
                        nombre = cursor.getString(idx)
                cursor.close()
        except Exception:
            pass
        return nombre or f"libro_{int(time.time())}.epub"

    def _resultado(self, request, result, data):
        if request != self.REQ:
            return
        self.activity.unbind(on_activity_result=self._resultado)
        copiados = []
        if result == -1 and data is not None:  # RESULT_OK
            uris = []
            clip = data.getClipData()
            if clip is not None:
                for i in range(clip.getItemCount()):
                    uris.append(clip.getItemAt(i).getUri())
            elif data.getData() is not None:
                uris.append(data.getData())
            resolver = self.ctx.getContentResolver()
            for uri in uris:
                nombre = self._nombre(uri)
                destino = os.path.join(self._destino, f"{uuid.uuid4().hex}_{nombre}")
                try:
                    entrada = resolver.openInputStream(uri)
                    salida = self.FileOutputStream(destino)
                    self.FileUtils.copy(entrada, salida)
                    salida.close()
                    entrada.close()
                    copiados.append((destino, nombre))
                except Exception as e:
                    print("Error copiando", nombre, e)
        cb = self._callback
        Clock.schedule_once(lambda dt: cb(copiados), 0)

    def guardar_en_descargas(self, ruta, nombre):
        """Copia el archivo a Descargas/Libros traducidos. Devuelve la carpeta visible."""
        mime = "application/epub+zip" if nombre.lower().endswith(".epub") else "text/plain"
        valores = self.ContentValues()
        valores.put("_display_name", nombre)
        valores.put("mime_type", mime)
        valores.put("relative_path", "Download/Libros traducidos")
        resolver = self.ctx.getContentResolver()
        uri = resolver.insert(self.Downloads.EXTERNAL_CONTENT_URI, valores)
        salida = resolver.openOutputStream(uri)
        entrada = self.FileInputStream(ruta)
        self.FileUtils.copy(entrada, salida)
        entrada.close()
        salida.close()
        return "Descargas › Libros traducidos"

    def pantalla_encendida(self, si):
        if not ANDROID:
            return
        from android.runnable import run_on_ui_thread

        @run_on_ui_thread
        def aplicar():
            ventana = self.ctx.getWindow()
            flag = self.LayoutParams.FLAG_KEEP_SCREEN_ON
            ventana.addFlags(flag) if si else ventana.clearFlags(flag)
        aplicar()


# ================================================================ Datos
class Biblioteca:
    def __init__(self, carpeta):
        self.carpeta = carpeta
        self.libros_dir = os.path.join(carpeta, "libros")
        self.importar_dir = os.path.join(carpeta, "importando")
        self.cache_dir = os.path.join(carpeta, "cache")
        for d in (self.libros_dir, self.importar_dir, self.cache_dir):
            os.makedirs(d, exist_ok=True)
        self.ruta = os.path.join(carpeta, "biblioteca.json")
        self.datos = {"libros": [], "estanterias": [TRADUCIDOS],
                      "ajustes": {"origen": "Detectar", "destino": "Inglés", "modo": "traducido"}}
        try:
            with open(self.ruta, encoding="utf-8") as f:
                self.datos.update(json.load(f))
        except Exception:
            pass

    def guardar(self):
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.datos, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.ruta)

    @property
    def libros(self):
        return self.datos["libros"]

    def estanterias(self):
        nombres = set(self.datos["estanterias"])
        nombres.update(l["estanteria"] for l in self.libros if l.get("estanteria"))
        return sorted(nombres, key=str.lower)

    def anadir(self, ruta_temporal, nombre, estanteria="", origen=None):
        ext = os.path.splitext(nombre)[1].lower()
        if ext not in (".epub", ".txt"):
            os.remove(ruta_temporal)
            return None
        id_libro = uuid.uuid4().hex[:12]
        destino = os.path.join(self.libros_dir, id_libro + ext)
        shutil.move(ruta_temporal, destino)
        titulo, autor = T.leer_metadatos(destino)
        libro = {"id": id_libro, "titulo": titulo, "autor": autor,
                 "formato": ext[1:].upper(), "archivo": destino,
                 "nombre_original": nombre, "estanteria": estanteria,
                 "anadido": time.strftime("%d/%m/%Y"), "origen": origen}
        self.libros.append(libro)
        self.guardar()
        return libro

    def buscar(self, id_libro):
        return next((l for l in self.libros if l["id"] == id_libro), None)

    def eliminar(self, id_libro):
        libro = self.buscar(id_libro)
        if libro:
            try:
                os.remove(libro["archivo"])
            except OSError:
                pass
            self.libros.remove(libro)
            self.guardar()


# ================================================================ Interfaz
KV = """
#:import dp kivy.metrics.dp
#:set FONDO (0.075, 0.07, 0.11, 1)
#:set SUPERFICIE (0.13, 0.12, 0.19, 1)
#:set ACENTO (0.95, 0.62, 0.42, 1)
#:set SUAVE (0.68, 0.66, 0.78, 1)
#:set TEXTO (0.96, 0.95, 0.98, 1)

<Label>:
    color: TEXTO

<Boton>:
    background_normal: ''
    background_down: ''
    background_color: (0,0,0,0)
    color: TEXTO
    font_size: '16sp'
    size_hint_y: None
    height: dp(52)
    fondo: SUPERFICIE
    canvas.before:
        Color:
            rgba: (self.fondo[0]*1.25, self.fondo[1]*1.25, self.fondo[2]*1.25, 1) if self.state == 'down' else self.fondo
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]

<BotonAcento>:
    fondo: ACENTO
    color: (0.1, 0.07, 0.1, 1)
    bold: True

<Campo@TextInput>:
    multiline: False
    size_hint_y: None
    height: dp(48)
    font_size: '16sp'
    padding: [dp(14), dp(13)]
    background_normal: ''
    background_active: ''
    background_color: SUPERFICIE
    foreground_color: TEXTO
    hint_text_color: SUAVE
    cursor_color: ACENTO

<Selector@Spinner>:
    size_hint_y: None
    height: dp(48)
    font_size: '15sp'
    background_normal: ''
    background_color: SUPERFICIE
    color: TEXTO

<Titulo@Label>:
    font_size: '24sp'
    bold: True
    size_hint_y: None
    height: dp(56)
    halign: 'left'
    valign: 'middle'
    text_size: self.size
    shorten: True

<Texto@Label>:
    size_hint_y: None
    height: self.texture_size[1] + dp(6)
    text_size: self.width, None
    halign: 'left'
    font_size: '15sp'

<FilaLibro>:
    orientation: 'horizontal'
    size_hint_y: None
    height: dp(84)
    padding: [dp(12), dp(8)]
    spacing: dp(12)
    canvas.before:
        Color:
            rgba: (0.18, 0.16, 0.26, 1) if self.state == 'down' else SUPERFICIE
        RoundedRectangle:
            pos: self.x, self.y + dp(4)
            size: self.width, self.height - dp(8)
            radius: [dp(12)]
    Label:
        size_hint_x: None
        width: dp(46)
        text: root.formato
        bold: True
        font_size: '12sp'
        color: (0.1, 0.07, 0.1, 1)
        canvas.before:
            Color:
                rgba: ACENTO if root.formato == 'EPUB' else SUAVE
            RoundedRectangle:
                pos: self.x, self.center_y - dp(30)
                size: self.width, dp(60)
                radius: [dp(4)]
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.titulo
            font_size: '16sp'
            bold: True
            halign: 'left'
            valign: 'bottom'
            text_size: self.size
            shorten: True
            shorten_from: 'right'
        Label:
            text: root.subtitulo
            font_size: '13sp'
            color: SUAVE
            halign: 'left'
            valign: 'top'
            text_size: self.size
            shorten: True

<PantallaBiblioteca>:
    canvas.before:
        Color:
            rgba: FONDO
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: [dp(16), dp(28), dp(16), dp(12)]
        spacing: dp(10)
        BoxLayout:
            size_hint_y: None
            height: dp(56)
            Titulo:
                text: 'Mis libros'
            BotonAcento:
                text: '+ Añadir'
                size_hint_x: None
                width: dp(120)
                on_release: app.importar()
        Campo:
            id: busqueda
            hint_text: 'Buscar por título o autor'
            on_text: root.filtrar()
        Selector:
            id: filtro
            text: app.TODAS
            values: [app.TODAS] + app.estanterias
            on_text: root.filtrar()
        Label:
            text: root.aviso
            color: SUAVE
            size_hint_y: None
            height: dp(22) if root.aviso else 0
            font_size: '13sp'
        RecycleView:
            id: lista
            viewclass: 'FilaLibro'
            RecycleBoxLayout:
                default_size: None, dp(84)
                default_size_hint: 1, None
                size_hint_y: None
                height: self.minimum_height
                orientation: 'vertical'
        Label:
            text: 'Toca «+ Añadir» para importar tus EPUB o TXT' if not app.libros_visibles and not busqueda.text else ''
            color: SUAVE
            size_hint_y: None
            height: dp(40) if self.text else 0

<PantallaLibro>:
    canvas.before:
        Color:
            rgba: FONDO
        Rectangle:
            pos: self.pos
            size: self.size
    ScrollView:
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: [dp(16), dp(28), dp(16), dp(16)]
            spacing: dp(12)
            Boton:
                text: '‹ Volver'
                size_hint_x: None
                width: dp(110)
                on_release: app.ir('biblioteca', 'right')
            Label:
                text: root.titulo
                font_size: '22sp'
                bold: True
                size_hint_y: None
                height: self.texture_size[1] + dp(8)
                text_size: self.width, None
                halign: 'left'
            Texto:
                text: root.autor
                color: SUAVE
            Texto:
                text: root.info
                color: SUAVE
                font_size: '13sp'
            BotonAcento:
                text: 'Traducir libro entero'
                on_release: app.abrir_traduccion(root.libro)
            Boton:
                text: 'Guardar copia en Descargas'
                on_release: app.exportar(root.libro)
            Texto:
                text: 'Estantería'
                bold: True
            Selector:
                id: estanteria
                text: root.estante
                values: [app.SIN_ESTANTERIA] + app.estanterias + [app.NUEVA]
                on_text: root.cambiar_estanteria(self.text)
            BoxLayout:
                size_hint_y: None
                height: dp(48) if root.creando else 0
                opacity: 1 if root.creando else 0
                disabled: not root.creando
                spacing: dp(8)
                Campo:
                    id: nueva
                    hint_text: 'Nombre de la estantería'
                Boton:
                    text: 'Crear'
                    size_hint_x: None
                    width: dp(90)
                    height: dp(48)
                    on_release: root.crear_estanteria(nueva.text)
            Widget:
                size_hint_y: None
                height: dp(16)
            Boton:
                text: '¿Seguro? Toca otra vez para eliminar' if root.confirmar else 'Eliminar libro'
                fondo: (0.55, 0.18, 0.22, 1) if root.confirmar else (0.22, 0.13, 0.17, 1)
                on_release: root.eliminar()

<PantallaTraducir>:
    canvas.before:
        Color:
            rgba: FONDO
        Rectangle:
            pos: self.pos
            size: self.size
    ScrollView:
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: [dp(16), dp(28), dp(16), dp(16)]
            spacing: dp(12)
            Boton:
                text: '‹ Volver'
                size_hint_x: None
                width: dp(110)
                disabled: root.trabajando
                on_release: app.ir('libro', 'right')
            Label:
                text: 'Traducir «%s»' % root.titulo
                font_size: '20sp'
                bold: True
                size_hint_y: None
                height: self.texture_size[1] + dp(8)
                text_size: self.width, None
                halign: 'left'
            Texto:
                text: 'Del idioma'
                color: SUAVE
            Selector:
                id: origen
                text: app.ajustes.get('origen', 'Detectar')
                values: app.IDIOMAS
                disabled: root.trabajando
            Texto:
                text: 'Al idioma'
                color: SUAVE
            Selector:
                id: destino
                text: app.ajustes.get('destino', 'Inglés')
                values: [i for i in app.IDIOMAS if i != 'Detectar']
                disabled: root.trabajando
            Texto:
                text: 'Resultado'
                color: SUAVE
            BoxLayout:
                size_hint_y: None
                height: dp(52)
                spacing: dp(8)
                ToggleButton:
                    id: modo_trad
                    group: 'modo'
                    text: 'Solo traducción'
                    state: 'down' if app.ajustes.get('modo', 'traducido') == 'traducido' else 'normal'
                    allow_no_selection: False
                    disabled: root.trabajando
                    background_normal: ''
                    background_down: ''
                    background_color: ACENTO if self.state == 'down' else SUPERFICIE
                    color: (0.1, 0.07, 0.1, 1) if self.state == 'down' else TEXTO
                ToggleButton:
                    id: modo_bil
                    group: 'modo'
                    text: 'Bilingüe'
                    state: 'down' if app.ajustes.get('modo') == 'bilingue' else 'normal'
                    allow_no_selection: False
                    disabled: root.trabajando
                    background_normal: ''
                    background_down: ''
                    background_color: ACENTO if self.state == 'down' else SUPERFICIE
                    color: (0.1, 0.07, 0.1, 1) if self.state == 'down' else TEXTO
            Texto:
                text: 'Bilingüe pone cada párrafo original con su traducción debajo. Ideal para aprender idiomas.' if modo_bil.state == 'down' else 'Crea un libro nuevo completamente traducido. El original no se toca.'
                color: SUAVE
                font_size: '13sp'
            BotonAcento:
                text: 'Traduciendo…' if root.trabajando else ('Continuar traducción' if root.error else 'Empezar a traducir')
                disabled: root.trabajando
                on_release: root.empezar(origen.text, destino.text, 'bilingue' if modo_bil.state == 'down' else 'traducido')
            ProgressBar:
                max: 1
                value: root.progreso
                size_hint_y: None
                height: dp(24)
            Texto:
                text: root.estado
                halign: 'center'
            Texto:
                text: root.espera
                color: (0.95, 0.8, 0.5, 1)
                font_size: '13sp'
                halign: 'center'
            Texto:
                text: root.error
                color: (1, 0.55, 0.55, 1)
                font_size: '13sp'
            Boton:
                text: 'Cancelar'
                opacity: 1 if root.trabajando else 0
                disabled: not root.trabajando
                on_release: root.cancelar()
            BotonAcento:
                text: 'Guardar en Descargas'
                opacity: 1 if root.resultado else 0
                disabled: not root.resultado
                on_release: app.exportar(app.biblio.buscar(root.resultado))
            Texto:
                text: 'Listo. El libro traducido ya está en tu biblioteca, en la estantería «Traducidos».' if root.resultado else 'Mantén la app abierta mientras traduce. Si se corta, vuelve a pulsar el botón y seguirá donde se quedó.'
                color: SUAVE
                font_size: '13sp'
"""


class Boton(Button):
    fondo = ListProperty([0.13, 0.12, 0.19, 1])


class BotonAcento(Boton):
    pass


class FilaLibro(ButtonBehavior, BoxLayout):
    id_libro = StringProperty("")
    titulo = StringProperty("")
    subtitulo = StringProperty("")
    formato = StringProperty("")

    def on_release(self):
        App.get_running_app().abrir_libro(self.id_libro)


class PantallaBiblioteca(Screen):
    aviso = StringProperty("")

    def filtrar(self, *_):
        app = App.get_running_app()
        q = self.ids.busqueda.text.strip().lower()
        estante = self.ids.filtro.text
        filas = []
        for l in sorted(app.biblio.libros, key=lambda x: x["titulo"].lower()):
            if q and q not in l["titulo"].lower() and q not in (l.get("autor") or "").lower():
                continue
            if estante != TODAS:
                if estante == SIN_ESTANTERIA and l.get("estanteria"):
                    continue
                if estante != SIN_ESTANTERIA and l.get("estanteria") != estante:
                    continue
            sub = l.get("autor") or "Autor desconocido"
            if l.get("estanteria"):
                sub += "  ·  " + l["estanteria"]
            filas.append({"id_libro": l["id"], "titulo": l["titulo"],
                          "subtitulo": sub, "formato": l["formato"]})
        self.ids.lista.data = filas
        app.libros_visibles = len(filas)


class ConLibro(Screen):
    libro = ObjectProperty({})
    titulo = StringProperty("")
    autor = StringProperty("")
    info = StringProperty("")
    estante = StringProperty(SIN_ESTANTERIA)

    def on_libro(self, *_):
        l = self.libro or {}
        self.titulo = l.get("titulo", "")
        self.autor = l.get("autor") or "Autor desconocido"
        self.info = "Formato: %s   ·   Añadido: %s" % (l.get("formato", ""), l.get("anadido", ""))
        self.estante = l.get("estanteria") or SIN_ESTANTERIA


class PantallaLibro(ConLibro):
    creando = BooleanProperty(False)
    confirmar = BooleanProperty(False)

    def on_pre_enter(self, *_):
        self.creando = False
        self.confirmar = False

    def cambiar_estanteria(self, texto):
        if not self.libro:
            return
        if texto == NUEVA:
            self.creando = True
            return
        self.creando = False
        app = App.get_running_app()
        libro = app.biblio.buscar(self.libro["id"])
        nuevo = "" if texto == SIN_ESTANTERIA else texto
        if libro and libro.get("estanteria", "") != nuevo:
            libro["estanteria"] = nuevo
            app.biblio.guardar()
            app.refrescar()

    def crear_estanteria(self, nombre):
        nombre = nombre.strip()
        if not nombre:
            return
        app = App.get_running_app()
        if nombre not in app.biblio.datos["estanterias"]:
            app.biblio.datos["estanterias"].append(nombre)
        app.biblio.guardar()
        app.refrescar()
        self.ids.nueva.text = ""
        self.creando = False
        self.ids.estanteria.text = nombre

    def eliminar(self):
        if not self.confirmar:
            self.confirmar = True
            return
        app = App.get_running_app()
        app.biblio.eliminar(self.libro["id"])
        app.refrescar()
        app.ir("biblioteca", "right")


class PantallaTraducir(ConLibro):
    trabajando = BooleanProperty(False)
    progreso = NumericProperty(0)
    estado = StringProperty("")
    error = StringProperty("")
    resultado = StringProperty("")
    espera = StringProperty("")

    def preparar(self, libro):
        if self.trabajando:
            return
        self.libro = libro
        self.progreso = 0
        self.estado = ""
        self.error = ""
        self.resultado = ""

    def empezar(self, origen, destino, modo):
        app = App.get_running_app()
        app.ajustes = {"origen": origen, "destino": destino, "modo": modo}
        app.biblio.datos["ajustes"] = dict(app.ajustes)
        app.biblio.guardar()
        self.error = ""
        self.resultado = ""
        self.espera = ""
        self.trabajando = True
        self.estado = "Preparando el libro…"
        self._cancelar = threading.Event()
        app.android.pantalla_encendida(True)
        threading.Thread(target=self._trabajo, daemon=True,
                         args=(dict(self.libro), T.IDIOMAS[origen], T.IDIOMAS[destino], modo)).start()

    def _trabajo(self, libro, origen, destino, modo):
        app = App.get_running_app()
        try:
            if origen != "auto" and origen == destino:
                raise T.MismoIdioma(origen)
            tr = T.Traductor(origen, destino, app.biblio.cache_dir,
                             aviso=self._aviso, cancelar=self._cancelar)
            tr.abrir_cache(libro["id"])
            ext = os.path.splitext(libro["archivo"])[1]
            tmp = os.path.join(app.biblio.importar_dir, f"trad_{libro['id']}{ext}")
            T.traducir_libro(libro["archivo"], tmp, tr, modo, self._avance, self._cancelar)
            base = os.path.splitext(libro.get("nombre_original") or libro["titulo"])[0]
            sufijo = f"_bilingue_{destino}" if modo == "bilingue" else f"_{destino}"
            self._terminado(tmp, base + sufijo + ext, libro["id"])
        except T.MismoIdioma:
            nombre = next((k for k, v in T.IDIOMAS.items() if v == destino), destino)
            self._fallo("El libro ya está en %s. Elige otro idioma en «Al idioma»." % nombre.lower(),
                        "No hace falta traducirlo")
        except T.Cancelado:
            self._fallo("", "Traducción en pausa. Pulsa «Continuar» cuando quieras.")
        except Exception as e:
            self._fallo(str(e), "Se ha detenido")

    @mainthread
    def _avance(self, valor, texto):
        self.progreso = valor
        self._ultimo = f"{texto}  ·  {int(valor * 100)} %"
        self.estado = self._ultimo
        self.espera = ""

    @mainthread
    def _aviso(self, texto):
        self.espera = texto

    @mainthread
    def _terminado(self, tmp, nombre, id_origen):
        app = App.get_running_app()
        nuevo = app.biblio.anadir(tmp, nombre, TRADUCIDOS, origen=id_origen)
        self.trabajando = False
        app.android.pantalla_encendida(False)
        self.progreso = 1
        self.estado = "¡Libro traducido!"
        self.resultado = nuevo["id"] if nuevo else ""
        app.refrescar()

    @mainthread
    def _fallo(self, error, estado):
        self.trabajando = False
        self.espera = ""
        App.get_running_app().android.pantalla_encendida(False)
        self.error = error or " "
        self.estado = estado

    def cancelar(self):
        if getattr(self, "_cancelar", None):
            self._cancelar.set()
            self.estado = "Pausando…"


class TraductorLibrosApp(App):
    TODAS = TODAS
    SIN_ESTANTERIA = SIN_ESTANTERIA
    NUEVA = NUEVA
    IDIOMAS = list(T.IDIOMAS.keys())
    estanterias = ListProperty([])
    libros_visibles = NumericProperty(0)
    ajustes = DictProperty({})

    def build(self):
        try:
            return self._build()
        except Exception:
            import traceback
            return pantalla_error(traceback.format_exc())

    def _build(self):
        self.title = "Traductor de Libros"
        self.biblio = Biblioteca(self.user_data_dir)
        self.ajustes = self.biblio.datos.get("ajustes", {})
        self.android = Android()
        Builder.load_string(KV)
        self.sm = ScreenManager(transition=SlideTransition(duration=0.2))
        self.sm.add_widget(PantallaBiblioteca(name="biblioteca"))
        self.sm.add_widget(PantallaLibro(name="libro"))
        self.sm.add_widget(PantallaTraducir(name="traducir"))
        from kivy.core.window import Window
        Window.bind(on_keyboard=self._tecla)
        Clock.schedule_once(lambda dt: self.refrescar(), 0)
        return self.sm

    def _tecla(self, ventana, tecla, *args):
        if tecla == 27:  # botón Atrás de Android
            actual = self.sm.current
            if actual == "libro":
                self.ir("biblioteca", "right")
                return True
            if actual == "traducir" and not self.sm.get_screen("traducir").trabajando:
                self.ir("libro", "right")
                return True
        return False

    def ir(self, pantalla, direccion="left"):
        self.sm.transition.direction = direccion
        self.sm.current = pantalla

    def refrescar(self):
        self.estanterias = self.biblio.estanterias()
        self.sm.get_screen("biblioteca").filtrar()

    def aviso(self, texto, segundos=4):
        pantalla = self.sm.get_screen("biblioteca")
        pantalla.aviso = texto
        Clock.schedule_once(lambda dt: setattr(pantalla, "aviso", ""), segundos)

    # ---- acciones
    def importar(self):
        if ANDROID:
            self.android.elegir_libros(self.biblio.importar_dir, self._importados)
        else:  # en el ordenador, para pruebas: importa de ./libros_prueba
            carpeta = os.path.join(os.path.dirname(__file__), "libros_prueba")
            copiados = []
            if os.path.isdir(carpeta):
                for n in os.listdir(carpeta):
                    tmp = os.path.join(self.biblio.importar_dir, uuid.uuid4().hex + "_" + n)
                    shutil.copy(os.path.join(carpeta, n), tmp)
                    copiados.append((tmp, n))
            self._importados(copiados)

    def _importados(self, archivos):
        n = 0
        for ruta, nombre in archivos:
            if self.biblio.anadir(ruta, nombre):
                n += 1
        self.refrescar()
        if archivos and n < len(archivos):
            self.aviso(f"Añadidos {n}. Solo se admiten archivos EPUB y TXT.")
        elif n:
            self.aviso(f"Añadido{'s' if n > 1 else ''} {n} libro{'s' if n > 1 else ''}")

    def abrir_libro(self, id_libro):
        libro = self.biblio.buscar(id_libro)
        if libro:
            self.sm.get_screen("libro").libro = libro
            self.ir("libro")

    def abrir_traduccion(self, libro):
        self.sm.get_screen("traducir").preparar(libro)
        self.ir("traducir")

    def exportar(self, libro):
        if not libro:
            return
        nombre = libro.get("nombre_original") or (libro["titulo"] + "." + libro["formato"].lower())
        try:
            if ANDROID:
                donde = self.android.guardar_en_descargas(libro["archivo"], nombre)
            else:
                donde = os.path.expanduser("~/Downloads")
                os.makedirs(donde, exist_ok=True)
                shutil.copy(libro["archivo"], os.path.join(donde, nombre))
            self.aviso(f"Guardado en {donde}")
            self.ir("biblioteca", "right")
        except Exception as e:
            self.aviso(f"No se pudo guardar: {e}", 6)


def pantalla_error(texto):
    from kivy.uix.textinput import TextInput
    return TextInput(text="La app ha encontrado un error al arrancar.\n"
                          "Haz una captura de pantalla y envíasela a Claude:\n\n" + texto,
                     readonly=True, font_size="13sp")


if __name__ == "__main__":
    TraductorLibrosApp().run()
