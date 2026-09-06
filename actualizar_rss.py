from __future__ import annotations

import email.utils
import html
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BASE = "https://efe.com"
SITEMAP_INDEX = f"{BASE}/sitemap_index.xml"

SALIDA = Path("rss.xml")
ESTADO = Path("estado.json")

NUMERO_SITEMAPS_RECIENTES = 3
NOTICIAS_PRIMERA_EJECUCION = 100
MAXIMO_ARTICULOS_RSS = 1500
MAXIMO_URL_ESTADO = 30000
MAXIMO_NUEVOS_POR_EJECUCION = 200
TRABAJADORES = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
)

CABECERAS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml,"
        "text/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Cache-Control": "no-cache",
}

NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
}


def descargar(url: str, timeout: int = 60) -> bytes:
    peticion = urllib.request.Request(
        url,
        headers=CABECERAS,
    )

    with urllib.request.urlopen(
        peticion,
        timeout=timeout,
    ) as respuesta:
        contenido = respuesta.read()

    if not contenido:
        raise RuntimeError(f"Respuesta vacía: {url}")

    return contenido


def limpiar_url(url: str) -> str:
    return url.strip().split("#", 1)[0].split("?", 1)[0]


def es_articulo(url: str) -> bool:
    if not url.startswith(f"{BASE}/"):
        return False

    ruta = urlparse(url).path.lower()

    exclusiones = (
        "/wp-admin/",
        "/wp-content/",
        "/author/",
        "/tag/",
        "/categoria/",
        "/contacto/",
        "/quienes-somos/",
        "/productos/",
        "/para-empresas/",
        "/para-medios/",
        "/politica-privacidad/",
    )

    if any(exclusion in ruta for exclusion in exclusiones):
        return False

    # Los artículos de EFE contienen el año en la dirección.
    return bool(
        re.search(r"/20\d{2}-\d{2}-\d{2}/", ruta)
    )


def numero_sitemap(url: str) -> int:
    coincidencia = re.search(
        r"/post-sitemap(\d+)\.xml$",
        url,
    )

    if coincidencia:
        return int(coincidencia.group(1))

    return 0


def localizar_sitemaps_recientes() -> list[str]:
    contenido = descargar(SITEMAP_INDEX)
    raiz = ET.fromstring(contenido)

    sitemaps: list[str] = []

    for nodo in raiz.findall("sm:sitemap", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        ).strip()

        if re.search(
            r"/post-sitemap\d+\.xml$",
            url,
        ):
            sitemaps.append(url)

    sitemaps.sort(
        key=numero_sitemap,
        reverse=True,
    )

    seleccionados = sitemaps[
        :NUMERO_SITEMAPS_RECIENTES
    ]

    print(
        "Sitemaps recientes encontrados: "
        f"{seleccionados}"
    )

    return seleccionados


def leer_sitemap(url_sitemap: str) -> dict[str, dict]:
    articulos: dict[str, dict] = {}

    try:
        contenido = descargar(url_sitemap)
        raiz = ET.fromstring(contenido)

    except Exception as error:
        print(
            f"No se pudo leer {url_sitemap}: {error}"
        )
        return articulos

    for nodo in raiz.findall("sm:url", NS):
        url = nodo.findtext(
            "sm:loc",
            default="",
            namespaces=NS,
        )

        url = limpiar_url(url)

        if not es_articulo(url):
            continue

        fecha = nodo.findtext(
            "sm:lastmod",
            default="",
            namespaces=NS,
        ).strip()

        imagen = nodo.findtext(
            "image:image/image:loc",
            default="",
            namespaces=NS,
        ).strip()

        articulos[url] = {
            "url": url,
            "fecha_sitemap": fecha,
            "imagen_sitemap": imagen,
        }

    return articulos


def localizar_articulos() -> dict[str, dict]:
    sitemaps = localizar_sitemaps_recientes()
    articulos: dict[str, dict] = {}

    with ThreadPoolExecutor(
        max_workers=min(
            TRABAJADORES,
            len(sitemaps),
        )
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                leer_sitemap,
                sitemap,
            ): sitemap
            for sitemap in sitemaps
        }

        for trabajo in as_completed(trabajos):
            articulos.update(trabajo.result())

    return articulos


def cargar_estado() -> tuple[set[str], bool]:
    if not ESTADO.exists():
        return set(), True

    try:
        datos = json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

        return set(datos.get("urls_vistas", [])), False

    except (json.JSONDecodeError, OSError):
        return set(), True


def guardar_texto_atomico(
    ruta: Path,
    contenido: str,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=".",
        prefix=f"{ruta.stem}_",
        suffix=".tmp",
    ) as temporal:
        temporal.write(contenido)
        ruta_temporal = Path(temporal.name)

    os.replace(ruta_temporal, ruta)


def guardar_estado(urls: set[str]) -> None:
    datos = {
        "ultima_actualizacion": datetime.now(
            timezone.utc
        ).isoformat(),
        "urls_vistas": sorted(urls)[-MAXIMO_URL_ESTADO:],
    }

    guardar_texto_atomico(
        ESTADO,
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
    )


def obtener_meta(
    sopa: BeautifulSoup,
    nombre: str,
    atributo: str = "property",
) -> str:
    etiqueta = sopa.find(
        "meta",
        attrs={atributo: nombre},
    )

    if not etiqueta:
        return ""

    return etiqueta.get("content", "").strip()


def convertir_fecha(fecha: str) -> datetime:
    if not fecha:
        return datetime.now(timezone.utc)

    fecha = fecha.strip().replace("Z", "+00:00")

    try:
        resultado = datetime.fromisoformat(fecha)

        if resultado.tzinfo is None:
            resultado = resultado.replace(
                tzinfo=timezone.utc
            )

        return resultado

    except ValueError:
        try:
            resultado = email.utils.parsedate_to_datetime(
                fecha
            )

            if resultado.tzinfo is None:
                resultado = resultado.replace(
                    tzinfo=timezone.utc
                )

            return resultado

        except (TypeError, ValueError):
            return datetime.now(timezone.utc)


def categoria_desde_url(url: str) -> str:
    ruta = urlparse(url).path.lower()

    categorias = [
        ("/economia/", "Economía y empresas"),
        ("/espana/", "España"),
        ("/mundo/", "Mundo"),
        ("/euro-efe/", "Europa"),
        ("/europa/", "Europa"),
        ("/cultura/", "Cultura"),
        ("/ciencia-y-tecnologia/", "Ciencia y tecnología"),
        ("/ciencia/", "Ciencia"),
        ("/tecnologia/", "Tecnología"),
        ("/deportes/", "Deportes"),
        ("/educacion/", "Educación"),
        ("/salud/", "Salud"),
        ("/medio-ambiente/", "Medio ambiente"),
        ("/efe-verde/", "Medio ambiente"),
        ("/elecciones/", "Elecciones"),
        ("/fotografia/", "Fotografía"),
    ]

    for patron, categoria in categorias:
        if patron in ruta:
            return categoria

    primera_carpeta = ruta.strip("/").split("/", 1)[0]

    if primera_carpeta:
        return primera_carpeta.replace("-", " ").title()

    return "EFE"


def extraer_categoria(
    sopa: BeautifulSoup,
    url: str,
) -> str:
    # WordPress suele indicar la sección dentro
    # de elementor-post-info__terms-list-item.
    categoria = sopa.select_one(
        ".elementor-post-info__terms-list-item"
    )

    if categoria:
        texto = categoria.get_text(
            " ",
            strip=True,
        )

        if texto:
            if texto.lower() == "economía":
                return "Economía y empresas"

            return texto

    return categoria_desde_url(url)


def extraer_articulo(
    url: str,
    datos_sitemap: dict,
) -> dict | None:
    try:
        contenido = descargar(url, timeout=45)
        sopa = BeautifulSoup(contenido, "html.parser")

        titulo = obtener_meta(sopa, "og:title")

        if not titulo:
            encabezado = sopa.find("h1")

            if encabezado:
                titulo = encabezado.get_text(
                    " ",
                    strip=True,
                )

        # Elimina el añadido habitual "- EFE".
        titulo = re.sub(
            r"\s*-\s*EFE\s*$",
            "",
            titulo,
            flags=re.IGNORECASE,
        )

        descripcion = (
            obtener_meta(sopa, "description", "name")
            or obtener_meta(sopa, "og:description")
        )

        fecha = (
            obtener_meta(
                sopa,
                "article:published_time",
            )
            or obtener_meta(
                sopa,
                "og:published_time",
            )
            or datos_sitemap.get(
                "fecha_sitemap",
                "",
            )
        )

        imagen = (
            obtener_meta(sopa, "og:image")
            or datos_sitemap.get(
                "imagen_sitemap",
                "",
            )
        )

        autor = obtener_meta(
            sopa,
            "author",
            "name",
        )

        titulo = html.unescape(
            " ".join(titulo.split())
        )

        descripcion = html.unescape(
            " ".join(descripcion.split())
        )

        if not titulo:
            return None

        return {
            "url": url,
            "titulo": titulo,
            "descripcion": descripcion,
            "fecha": convertir_fecha(fecha),
            "categoria": extraer_categoria(
                sopa,
                url,
            ),
            "autor": autor,
            "imagen": imagen,
        }

    except Exception as error:
        print(f"No se pudo procesar {url}: {error}")
        return None


def texto_elemento(
    elemento: ET.Element,
    nombre: str,
) -> str:
    nodo = elemento.find(nombre)

    if nodo is None or nodo.text is None:
        return ""

    return nodo.text.strip()


def cargar_rss_anterior() -> dict[str, ET.Element]:
    articulos: dict[str, ET.Element] = {}

    if not SALIDA.exists():
        return articulos

    try:
        raiz = ET.parse(SALIDA).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return articulos

        for item in canal.findall("item"):
            url = (
                texto_elemento(item, "guid")
                or texto_elemento(item, "link")
            )

            url = limpiar_url(url)

            if url:
                articulos[url] = item

    except ET.ParseError:
        print("El rss.xml anterior no era válido")

    return articulos


def fecha_item(item: ET.Element) -> datetime:
    return convertir_fecha(
        texto_elemento(item, "pubDate")
    )


def crear_item(datos: dict) -> ET.Element:
    item = ET.Element("item")

    ET.SubElement(item, "title").text = datos["titulo"]
    ET.SubElement(item, "link").text = datos["url"]

    guid = ET.SubElement(
        item,
        "guid",
        {"isPermaLink": "true"},
    )
    guid.text = datos["url"]

    ET.SubElement(item, "pubDate").text = (
        email.utils.format_datetime(datos["fecha"])
    )

    ET.SubElement(item, "category").text = (
        datos["categoria"]
    )

    if datos.get("descripcion"):
        ET.SubElement(item, "description").text = (
            datos["descripcion"]
        )

    if datos.get("autor"):
        ET.SubElement(item, "author").text = (
            datos["autor"]
        )

    if datos.get("imagen"):
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": datos["imagen"],
                "type": "image/jpeg",
            },
        )

    return item


def crear_rss(
    articulos: dict[str, ET.Element],
) -> ET.ElementTree:
    ordenados = sorted(
        articulos.values(),
        key=fecha_item,
        reverse=True,
    )[:MAXIMO_ARTICULOS_RSS]

    rss = ET.Element("rss", {"version": "2.0"})
    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "EFE — Todas las noticias"
    )

    ET.SubElement(canal, "link").text = BASE

    ET.SubElement(canal, "description").text = (
        "Todas las noticias públicas de EFE, incluidas "
        "Economía, empresas, España, Mundo y Europa."
    )

    ET.SubElement(canal, "language").text = "es-ES"

    ET.SubElement(canal, "lastBuildDate").text = (
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        )
    )

    for item in ordenados:
        canal.append(item)

    return ET.ElementTree(rss)


def guardar_xml_atomico(arbol: ET.ElementTree) -> None:
    ET.indent(arbol, space="  ")

    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=".",
        prefix="rss_",
        suffix=".xml",
    ) as temporal:
        ruta_temporal = Path(temporal.name)

        arbol.write(
            temporal,
            encoding="utf-8",
            xml_declaration=True,
        )

    os.replace(ruta_temporal, SALIDA)


def main() -> None:
    localizados = localizar_articulos()

    urls_vistas, primera_ejecucion = cargar_estado()
    articulos = cargar_rss_anterior()

    if primera_ejecucion:
        ordenados = sorted(
            localizados.items(),
            key=lambda elemento: convertir_fecha(
                elemento[1].get(
                    "fecha_sitemap",
                    "",
                )
            ),
            reverse=True,
        )

        seleccionados = ordenados[
            :NOTICIAS_PRIMERA_EJECUCION
        ]

        candidatos = {
            url: datos
            for url, datos in seleccionados
        }

        # Registra el resto para evitar introducir
        # miles de noticias antiguas en Feedly.
        urls_vistas.update(localizados)

        print(
            "Primera ejecución: se añadirán "
            f"{len(candidatos)} noticias recientes"
        )

    else:
        nuevas_urls = (
            set(localizados)
            - urls_vistas
        )

        candidatos = {
            url: localizados[url]
            for url in nuevas_urls
        }

        print(
            f"Nuevas noticias detectadas: "
            f"{len(candidatos)}"
        )

        nuevas_economia = sum(
            1
            for url in candidatos
            if "/economia/" in url.lower()
        )

        print(
            "Nuevas noticias de Economía/empresas: "
            f"{nuevas_economia}"
        )

    pendientes = [
        (url, datos)
        for url, datos in candidatos.items()
        if url not in articulos
    ]

    pendientes.sort(
        key=lambda elemento: convertir_fecha(
            elemento[1].get(
                "fecha_sitemap",
                "",
            )
        ),
        reverse=True,
    )

    pendientes = pendientes[
        :MAXIMO_NUEVOS_POR_EJECUCION
    ]

    resultados: list[dict] = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:
        trabajos = {
            ejecutor.submit(
                extraer_articulo,
                url,
                datos,
            ): url
            for url, datos in pendientes
        }

        for trabajo in as_completed(trabajos):
            resultado = trabajo.result()

            if resultado:
                resultados.append(resultado)

    for resultado in resultados:
        articulos[resultado["url"]] = crear_item(
            resultado
        )

        urls_vistas.add(resultado["url"])

    urls_vistas.update(articulos.keys())

    guardar_xml_atomico(
        crear_rss(articulos)
    )

    guardar_estado(urls_vistas)

    economia_anadidas = sum(
        1
        for resultado in resultados
        if resultado["categoria"].lower()
        in ("economía", "economía y empresas")
    )

    print(
        f"RSS actualizado: {len(articulos)} noticias"
    )

    print(
        f"Nuevas añadidas: {len(resultados)}"
    )

    print(
        "Nuevas de Economía/empresas añadidas: "
        f"{economia_anadidas}"
    )


if __name__ == "__main__":
    main()
