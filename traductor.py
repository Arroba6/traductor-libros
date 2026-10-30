"""
Motor de traducción de libros completos (EPUB y TXT).

Funciona como el plugin "Ebook Translator" de Calibre:
- Recorre todos los capítulos del EPUB en orden.
- Traduce cada párrafo, título, elemento de lista, etc.
- Conserva la estructura, las imágenes y el estilo del libro.
- Modo "traducido" (solo la traducción) o "bilingue" (original + traducción).
- Guarda lo ya traducido en una caché, así si se corta puede continuar.
"""
import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from posixpath import dirname, join, normpath

from bs4 import BeautifulSoup

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL = ssl.create_default_context()


IDIOMAS = {
    "Detectar": "auto",
    "Español": "es",
    "Inglés": "en",
    "Catalán": "ca",
    "Francés": "fr",
    "Alemán": "de",
    "Italiano": "it",
    "Portugués": "pt",
    "Japonés": "ja",
    "Coreano": "ko",
    "Chino": "zh-CN",
    "Ruso": "ru",
}

LIMITE = 4000          # caracteres por petición
BLOQUES = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
           "dt", "dd", "figcaption", "caption", "td", "th", "div"]
_TIENE_LETRAS = re.compile(r"[^\W\d_]", re.UNICODE)


class Cancelado(Exception):
    pass


# ---------------------------------------------------------------- traductor
class Traductor:
    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, origen, destino, carpeta_cache, prueba=False):
        self.origen, self.destino = origen, destino
        self.prueba = prueba
        self.carpeta_cache = carpeta_cache
        self.cache = {}
        self.ruta_cache = None

    # caché por libro e idiomas
    def abrir_cache(self, id_libro):
        os.makedirs(self.carpeta_cache, exist_ok=True)
        self.ruta_cache = os.path.join(
            self.carpeta_cache, f"{id_libro}_{self.origen}_{self.destino}.json")
        try:
            with open(self.ruta_cache, encoding="utf-8") as f:
                self.cache = json.load(f)
        except Exception:
            self.cache = {}

    def guardar_cache(self):
        if not self.ruta_cache:
            return
        tmp = self.ruta_cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(tmp, self.ruta_cache)

    @staticmethod
    def _clave(t):
        return hashlib.sha1(t.encode("utf-8")).hexdigest()

    def _peticion(self, texto):
        if self.prueba:
            return "\n".join(f"[{self.destino}] {l}" for l in texto.split("\n"))
        datos = urllib.parse.urlencode({
            "client": "gtx", "sl": self.origen, "tl": self.destino,
            "dt": "t", "q": texto}).encode("utf-8")
        req = urllib.request.Request(self.URL, data=datos, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14)",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        ultimo = None
        for intento in range(8):
            try:
                with urllib.request.urlopen(req, timeout=40, context=_SSL) as r:
                    j = json.loads(r.read().decode("utf-8"))
                time.sleep(0.25)
                return "".join(s[0] for s in (j[0] or []) if s and s[0])
            except urllib.error.HTTPError as e:
                ultimo = e
                time.sleep(10 * (intento + 1) if e.code == 429 else 2 * (intento + 1))
            except Exception as e:
                ultimo = e
                time.sleep(3 * (intento + 1))
        raise RuntimeError(
            "No se pudo conectar con el traductor. Revisa tu conexión a internet "
            f"y vuelve a pulsar Traducir: continuará donde se quedó. ({ultimo})")

    def _texto_largo(self, t):
        if len(t) <= LIMITE:
            return self._peticion(t).strip()
        frases = re.split(r"(?<=[.!?…;:])\s+", t)
        trozos, actual = [], ""
        for fr in frases:
            if len(actual) + len(fr) + 1 > LIMITE and actual:
                trozos.append(actual)
                actual = ""
            while len(fr) > LIMITE:
                trozos.append(fr[:LIMITE])
                fr = fr[LIMITE:]
            actual = (actual + " " + fr).strip()
        if actual:
            trozos.append(actual)
        return " ".join(self._peticion(x).strip() for x in trozos)

    def traducir_lista(self, textos, avance=None, cancelar=None):
        """Traduce una lista de textos agrupándolos en pocas peticiones."""
        salida = [None] * len(textos)
        pendientes = []
        for i, t in enumerate(textos):
            k = self._clave(t)
            if k in self.cache:
                salida[i] = self.cache[k]
            else:
                pendientes.append(i)
        if avance:
            avance(sum(len(textos[i]) for i in range(len(textos)) if salida[i] is not None))

        def enviar(grupo):
            if not grupo:
                return
            if cancelar and cancelar.is_set():
                raise Cancelado()
            if len(grupo) == 1:
                res = [self._texto_largo(textos[grupo[0]])]
            else:
                partes = self._peticion("\n".join(textos[i] for i in grupo)).split("\n")
                partes = [p.strip() for p in partes]
                if len(partes) != len(grupo):
                    res = [self._texto_largo(textos[i]) for i in grupo]
                else:
                    res = partes
            for i, r in zip(grupo, res):
                salida[i] = r
                self.cache[self._clave(textos[i])] = r
            if avance:
                avance(sum(len(textos[i]) for i in grupo))

        grupo, tam = [], 0
        for i in pendientes:
            t = textos[i]
            if len(t) > LIMITE:
                enviar(grupo)
                grupo, tam = [], 0
                enviar([i])
                continue
            if tam + len(t) + 1 > LIMITE or len(grupo) >= 50:
                enviar(grupo)
                grupo, tam = [], 0
            grupo.append(i)
            tam += len(t) + 1
        enviar(grupo)
        return salida


# ---------------------------------------------------------------- EPUB
def _atributos(etiqueta):
    return {k.split(":")[-1]: (v1 or v2) for k, v1, v2 in
            re.findall(r'([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', etiqueta)}


def _ruta_opf(z):
    contenedor = z.read("META-INF/container.xml").decode("utf-8", "replace")
    m = re.search(r'full-path\s*=\s*"([^"]+)"', contenedor)
    if not m:
        raise ValueError("El EPUB no tiene un índice válido (container.xml).")
    return m.group(1)


def _capitulos(z, opf_ruta, opf):
    base = dirname(opf_ruta)
    items = {}
    for etiqueta in re.findall(r"<(?:\w+:)?item\b[^>]*>", opf):
        a = _atributos(etiqueta)
        if "id" in a and "href" in a:
            items[a["id"]] = (normpath(join(base, urllib.parse.unquote(a["href"]))),
                              a.get("media-type", ""))
    orden = []
    for etiqueta in re.findall(r"<(?:\w+:)?itemref\b[^>]*>", opf):
        a = _atributos(etiqueta)
        ruta, tipo = items.get(a.get("idref"), (None, ""))
        if ruta and ("html" in tipo or ruta.endswith((".xhtml", ".html", ".htm"))):
            if ruta in z.namelist() and ruta not in orden:
                orden.append(ruta)
    return orden


def leer_metadatos(ruta):
    """Devuelve (titulo, autor) del libro."""
    nombre = os.path.splitext(os.path.basename(ruta))[0]
    if not ruta.lower().endswith(".epub"):
        return nombre, ""
    try:
        with zipfile.ZipFile(ruta) as z:
            opf = z.read(_ruta_opf(z)).decode("utf-8", "replace")
        t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
        a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
        limpiar = lambda s: BeautifulSoup(s, "html.parser").get_text().strip()
        return (limpiar(t.group(1)) if t else nombre) or nombre, limpiar(a.group(1)) if a else ""
    except Exception:
        return nombre, ""


def _bloques(sopa):
    cuerpo = sopa.body or sopa
    resultado = []
    for el in cuerpo.find_all(BLOQUES):
        if el.find(BLOQUES):          # solo bloques "hoja" (sin otros bloques dentro)
            continue
        if el.find_parent(["script", "style", "svg", "math"]):
            continue
        texto = " ".join(el.get_text().split())
        if texto and _TIENE_LETRAS.search(texto):
            resultado.append((el, texto))
    return resultado


def _aplicar(sopa, el, traduccion, modo):
    if modo == "bilingue":
        if el.name in ("td", "th", "li", "dt", "dd", "caption"):
            el.append(sopa.new_tag("br"))
            span = sopa.new_tag("span", attrs={"class": "traduccion",
                                               "style": "opacity:0.75;font-style:italic"})
            span.string = traduccion
            el.append(span)
        else:
            attrs = {k: v for k, v in el.attrs.items() if k != "id"}
            nuevo = sopa.new_tag(el.name, attrs=attrs)
            estilo = attrs.get("style", "")
            nuevo["style"] = (estilo + ";" if estilo else "") + "opacity:0.75;font-style:italic"
            nuevo.string = traduccion
            el.insert_after(nuevo)
    else:
        el.clear()
        el.append(traduccion)


def traducir_epub(entrada, salida, traductor, modo="traducido",
                  progreso=None, cancelar=None):
    with zipfile.ZipFile(entrada) as z:
        opf_ruta = _ruta_opf(z)
        opf = z.read(opf_ruta).decode("utf-8", "replace")
        capitulos = _capitulos(z, opf_ruta, opf)
        sopas = {}
        for ruta in capitulos:
            html = z.read(ruta).decode("utf-8", "replace")
            sopa = BeautifulSoup(html, "html.parser")
            bl = _bloques(sopa)
            if bl:
                sopas[ruta] = (sopa, bl)
        total = sum(len(t) for s in sopas.values() for _, t in s[1]) or 1
        hecho = [0]

        def avance(n):
            hecho[0] += n
            if progreso:
                progreso(min(hecho[0] / total, 1.0), estado[0])

        estado = [""]
        modificados = {}
        try:
            for n, (ruta, (sopa, bl)) in enumerate(sopas.items(), 1):
                estado[0] = f"Capítulo {n} de {len(sopas)}"
                if progreso:
                    progreso(min(hecho[0] / total, 1.0), estado[0])
                trad = traductor.traducir_lista([t for _, t in bl], avance, cancelar)
                for (el, _), tr in zip(bl, trad):
                    _aplicar(sopa, el, tr, modo)
                modificados[ruta] = str(sopa).encode("utf-8")
                traductor.guardar_cache()
        finally:
            traductor.guardar_cache()

        # Actualizar idioma y título en los metadatos
        dest = traductor.destino
        opf_nuevo = re.sub(r"(<dc:language[^>]*>)(.*?)(</dc:language>)",
                           lambda m: m.group(1) + (dest if modo != "bilingue" else m.group(2)) + m.group(3),
                           opf, count=1, flags=re.S)
        etiqueta = f" [{dest.upper()}]" if modo != "bilingue" else f" [Bilingüe {dest.upper()}]"
        opf_nuevo = re.sub(r"(<dc:title[^>]*>)(.*?)(</dc:title>)",
                           lambda m: m.group(1) + m.group(2).strip() + etiqueta + m.group(3),
                           opf_nuevo, count=1, flags=re.S)
        modificados[opf_ruta] = opf_nuevo.encode("utf-8")

        tmp = salida + ".tmp"
        with zipfile.ZipFile(tmp, "w") as out:
            if "mimetype" in z.namelist():
                out.writestr(zipfile.ZipInfo("mimetype"), z.read("mimetype"),
                             compress_type=zipfile.ZIP_STORED)
            for info in z.infolist():
                if info.filename == "mimetype":
                    continue
                datos = modificados.get(info.filename)
                if datos is None:
                    datos = z.read(info.filename)
                out.writestr(info.filename, datos, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp, salida)
    if progreso:
        progreso(1.0, "¡Terminado!")
    return salida


# ---------------------------------------------------------------- TXT
def traducir_txt(entrada, salida, traductor, modo="traducido",
                 progreso=None, cancelar=None):
    with open(entrada, "rb") as f:
        crudo = f.read()
    for cod in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            texto = crudo.decode(cod)
            break
        except Exception:
            continue
    parrafos = re.split(r"\n\s*\n", texto.replace("\r\n", "\n"))
    indices = [i for i, p in enumerate(parrafos) if _TIENE_LETRAS.search(p)]
    textos = [" ".join(parrafos[i].split()) for i in indices]
    total = sum(len(t) for t in textos) or 1
    hecho = [0]

    def avance(n):
        hecho[0] += n
        if progreso:
            progreso(min(hecho[0] / total, 1.0), "Traduciendo texto")

    try:
        trad = traductor.traducir_lista(textos, avance, cancelar)
    finally:
        traductor.guardar_cache()
    for i, tr in zip(indices, trad):
        parrafos[i] = parrafos[i].strip() + "\n\n" + tr if modo == "bilingue" else tr
    with open(salida, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parrafos))
    if progreso:
        progreso(1.0, "¡Terminado!")
    return salida


def traducir_libro(entrada, salida, traductor, modo="traducido", progreso=None, cancelar=None):
    if entrada.lower().endswith(".epub"):
        return traducir_epub(entrada, salida, traductor, modo, progreso, cancelar)
    return traducir_txt(entrada, salida, traductor, modo, progreso, cancelar)
