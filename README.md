# CrossSpice para tabletas (PWA)

Editor y simulador compatible con LTspice que corre entero en el navegador y
funciona sin conexión una vez instalado. Mismo núcleo Python que la versión de
escritorio (`crossspice/*.py`) en **Pyodide 0.27.2**, y **ngspice 45.2** en
WebAssembly (`eecircuit-engine` 1.8.0). Todo va alojado en esta carpeta: no usa
CDN.

## Probarla en la PC

```bash
python tests/web_server.py 8771
```

Abrir <http://localhost:8771/web/index.html>. Ese servidor también entrega, bajo
`/ltspice/`, el `lib.zip` y los ejemplos del LTspice instalado (solo para
pruebas; la app no los usa).

## Publicarla

1. `python web/build.py` — copia el núcleo a `web/py/crossspice/`, regenera la
   librería básica y escribe `precache.json` y la versión del service worker.
   **Correrlo cada vez que cambie el núcleo o algo de `web/`.**
2. Subir la carpeta `web/` tal cual a cualquier hosting HTTPS (GitHub Pages,
   el servidor del TEC…). Unos 35 MB; el service worker los guarda en la tableta
   la primera vez.
3. En la tableta (Chrome para Android): abrir la dirección y "Instalar app" o
   "Agregar a la pantalla principal".

## Uso en la tableta

| Gesto | Acción |
|---|---|
| Tocar | Elegir (o colocar / poner cable / sondear, según la herramienta) |
| Mantener presionado | Editar (valor, modelo, nombre de red, directiva) |
| Arrastrar con un dedo | Mover lo elegido (los cables conectados se estiran) o desplazar la vista |
| Pellizcar / dos dedos | Zoom / desplazar |
| Tras simular, tocar un cable | Graficar su voltaje; tocar una resistencia o fuente grafica su corriente |
| En la gráfica | Tocar = cursor con valores; pellizcar o arrastrar = zoom/desplazar en x; doble toque = ver todo; tocar un nombre = quitar la traza |

Los esquemáticos se guardan en la tableta (IndexedDB) y se exportan como `.asc`
con la codificación de LTspice. El documento abierto se guarda solo.

**Librería:** trae una básica propia (`lib/basic`, generada por
`lib/make_basic.py`: R, C, L, diodo, zener, fuentes V/I/B, NPN/PNP, MOSFET y
opamp ideal, más algunos modelos comunes). Para todos los componentes de
LTspice, el estudiante importa su propio `lib.zip` (Archivo → Librería); se
guarda en la tableta y se indexa sin descomprimirlo (≈0,7 s).

## Arquitectura

| Archivo | Qué hace |
|---|---|
| `core-worker.js` + `py/webcore.py` | Worker con Pyodide: el esquemático vive aquí; edición, símbolos, conectividad y netlist con el núcleo de escritorio |
| `spice-worker.js` | Worker con ngspice. Si una corrida falla lo detecta al instante; si se cuelga o se pulsa Detener, la interfaz descarta el worker y crea otro |
| `editor.js` | Dibujo SVG con la paleta de LTspice y gestos táctiles |
| `plot.js` | Gráficas (.tran, .dc, .ac con magnitud y fase, .step) |
| `app.js` | Barra de herramientas, diálogos, archivos, simulación y sondas |
| `sw.js`, `manifest.webmanifest` | PWA sin conexión |

## Limitaciones conocidas

- ngspice-WASM sin XSPICE: el diodo ideal se aproxima (`replace_ideal_diodes`).
- No escribe corrientes internas de dispositivos (`@x[i]`): la sonda calcula la
  de las resistencias como ΔV/R y lee la de las fuentes de voltaje; las demás no.
- Solo se grafican .tran, .ac, .dc y .op (.noise/.tf no).
- La netlist se pasa a ASCII antes de simular (ngspice-WASM falla con tildes).
- ~60 % de los modelos de ADI vienen cifrados: solo LTspice puede simularlos.
- No se ha medido en una tableta real; en PC Python arranca en ~4 s (1,7 s desde
  la caché) y los ejemplos simulan en < 0,1 s.
