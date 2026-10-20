"""Genera la librería básica incluida en CrossSpice Web (web/lib/basic/).

Todos los dibujos y modelos son propios de CrossSpice. Solo las posiciones de
los pines y de los atributos coinciden con LTspice, para que los .asc hechos
en LTspice abran y se conecten igual.

Uso:  python web/lib/make_basic.py
"""
import json
import os

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "basic")


def pins(*spec):
    out = []
    for i, (x, y, name) in enumerate(spec, 1):
        out += [f"PIN {x} {y} NONE 0", f"PINATTR PinName {name}", f"PINATTR SpiceOrder {i}"]
    return out


def sym(lines, windows, attrs, pin_spec):
    out = ["Version 4", "SymbolType CELL"] + lines
    out += [f"WINDOW {w}" for w in windows]
    out += [f"SYMATTR {k} {v}" for k, v in attrs]
    return "\n".join(out + pins(*pin_spec)) + "\n"


def L(*c):
    return "LINE Normal " + " ".join(str(v) for v in c)


ZIG = [L(16, 16, 16, 28), L(16, 28, 4, 32), L(4, 32, 28, 40), L(28, 40, 4, 48),
       L(4, 48, 28, 56), L(28, 56, 4, 64), L(4, 64, 28, 72), L(28, 72, 4, 80),
       L(4, 80, 16, 84), L(16, 84, 16, 96)]
DIODE = [L(16, 0, 16, 20), L(0, 20, 32, 20), L(32, 20, 16, 44), L(16, 44, 0, 20),
         L(0, 44, 32, 44), L(16, 44, 16, 64)]
VSRC = [L(0, 16, 0, 24), "CIRCLE Normal -32 24 32 88", L(0, 88, 0, 96),
        L(-8, 40, 8, 40), L(0, 32, 0, 48), L(-8, 72, 8, 72)]
BJT = [L(0, 48, 16, 48), L(16, 16, 16, 80), L(16, 36, 64, 4), L(64, 4, 64, 0),
       L(16, 60, 64, 92), L(64, 92, 64, 96)]
MOS = [L(0, 80, 16, 80), L(16, 80, 16, 16), L(24, 16, 24, 32), L(24, 40, 24, 56),
       L(24, 64, 24, 80), L(24, 24, 48, 24), L(48, 24, 48, 0), L(24, 72, 48, 72),
       L(48, 72, 48, 96), L(24, 48, 48, 48)]

SYMBOLS = {
    "res": sym(ZIG, ["0 36 40 Left 2", "3 36 76 Left 2"],
               [("Value", "R"), ("Prefix", "R"), ("Description", "Resistencia")],
               [(16, 16, "A"), (16, 96, "B")]),
    "cap": sym([L(16, 0, 16, 26), L(0, 26, 32, 26), L(0, 38, 32, 38), L(16, 38, 16, 64)],
               ["0 24 8 Left 2", "3 24 56 Left 2"],
               [("Value", "C"), ("Prefix", "C"), ("Description", "Capacitor")],
               [(16, 0, "A"), (16, 64, "B")]),
    "ind": sym([L(16, 16, 16, 24)] +
               [f"ARC Normal 6 {y} 26 {y + 16} 16 {y + 16} 16 {y}" for y in (24, 40, 56, 72)] +
               [L(16, 88, 16, 96)],
               ["0 36 40 Left 2", "3 36 80 Left 2"],
               [("Value", "L"), ("Prefix", "L"), ("Description", "Inductor")],
               [(16, 16, "A"), (16, 96, "B")]),
    "diode": sym(DIODE, ["0 24 0 Left 2", "3 24 64 Left 2"],
                 [("Value", "D"), ("Prefix", "D"), ("Description", "Diodo")],
                 [(16, 0, "+"), (16, 64, "-")]),
    "zener": sym(DIODE + [L(0, 44, -6, 38), L(32, 44, 38, 50)],
                 ["0 24 0 Left 2", "3 24 64 Left 2"],
                 [("Value", "D"), ("Prefix", "D"), ("Description", "Diodo Zener")],
                 [(16, 0, "+"), (16, 64, "-")]),
    "voltage": sym(VSRC, ["0 24 16 Left 2", "3 24 96 Left 2"],
                   [("Value", "V"), ("Prefix", "V"),
                    ("Description", "Fuente de voltaje: DC, AC, PULSE, SINE, PWL, EXP o SFFM")],
                   [(0, 16, "+"), (0, 96, "-")]),
    "current": sym([L(0, 0, 0, 16), "CIRCLE Normal -24 16 24 64", L(0, 64, 0, 80),
                    L(0, 26, 0, 54), L(0, 54, -6, 44), L(0, 54, 6, 44)],
                   ["0 24 0 Left 2", "3 24 80 Left 2"],
                   [("Value", "I"), ("Prefix", "I"),
                    ("Description", "Fuente de corriente: DC, AC, PULSE, SINE, PWL, EXP o SFFM")],
                   [(0, 0, "+"), (0, 80, "-")]),
    "bv": sym(VSRC + ["TEXT -14 56 Left 1 B"], ["0 24 16 Left 2", "3 24 96 Left 2"],
              [("Value", "V=0"), ("Prefix", "B"),
               ("Description", "Fuente de voltaje comportamental (expresión)")],
              [(0, 16, "+"), (0, 96, "-")]),
    "npn": sym(BJT + [L(64, 92, 46, 89), L(64, 92, 56, 76)],
               ["0 56 32 Left 2", "3 56 68 Left 2"],
               [("Value", "NPN"), ("Prefix", "QN"), ("Description", "Transistor bipolar NPN")],
               [(64, 0, "C"), (0, 48, "B"), (64, 96, "E")]),
    "pnp": sym(BJT + [L(16, 60, 34, 63), L(16, 60, 24, 76)],
               ["0 84 32 Left 2", "3 84 68 Left 2"],
               [("Value", "PNP"), ("Prefix", "QP"), ("Description", "Transistor bipolar PNP")],
               [(64, 0, "C"), (0, 48, "B"), (64, 96, "E")]),
    "nmos": sym(MOS + [L(48, 48, 48, 72), L(24, 48, 34, 42), L(24, 48, 34, 54)],
                ["0 56 32 Left 2", "3 56 72 Left 2"],
                [("Value", "NMOS"), ("Prefix", "MN"), ("Description", "MOSFET canal N")],
                [(48, 0, "D"), (0, 80, "G"), (48, 96, "S")]),
    "pmos": sym(MOS + [L(48, 48, 48, 24), L(48, 48, 38, 42), L(48, 48, 38, 54)],
                ["0 56 32 Left 2", "3 56 72 Left 2"],
                [("Value", "PMOS"), ("Prefix", "MP"), ("Description", "MOSFET canal P")],
                [(48, 0, "D"), (0, 80, "G"), (48, 96, "S")]),
    "OpAmps/opamp": sym([L(-32, 32, 32, 64), L(32, 64, -32, 96), L(-32, 96, -32, 32),
                         L(-24, 48, -12, 48), L(-24, 80, -12, 80), L(-18, 74, -18, 86)],
                        ["0 0 32 Left 2"],
                        [("Prefix", "X"),
                         ("Description", "Amplificador operacional ideal de un polo (Aol, GBW). "
                                         "Requiere .lib opamp.sub"),
                         ("Value", "opamp"), ("SpiceLine", "Aol=100K"),
                         ("SpiceLine2", "GBW=10Meg")],
                        [(-32, 48, "invin"), (-32, 80, "noninvin"), (32, 64, "out")]),
}

FILES = {
    "sub/opamp.sub": """\
* Amplificador operacional ideal de un polo (CrossSpice, librería básica).
* Mismos nodos y parámetros que el símbolo OpAmps\\opamp de LTspice:
* invin noninvin out, Aol (ganancia en lazo abierto), GBW (ganancia-ancho de banda).
.subckt opamp invin noninvin out Aol=100K GBW=10Meg
G1 0 n1 noninvin invin 1m
R1 n1 0 {Aol*1k}
C1 n1 0 {Aol/(6.283185307*GBW*1k)}
E1 out 0 n1 0 1
.ends opamp
""",
    "cmp/standard.dio": """\
* Diodos genéricos (CrossSpice, librería básica; parámetros típicos publicados).
.model 1N4148 D(Is=4.352n N=1.906 Rs=0.6458 Cjo=7.048p M=0.285 Vj=0.869 BV=100 IBV=100u TT=3.48n)
.model 1N914 D(Is=4.352n N=1.906 Rs=0.6458 Cjo=7.048p M=0.285 Vj=0.869 BV=100 IBV=100u TT=3.48n)
.model 1N4001 D(Is=14.11n N=1.984 Rs=33.89m Cjo=25.89p M=0.44 Vj=0.3245 BV=75 IBV=10u TT=5.7u)
.model 1N4007 D(Is=14.11n N=1.984 Rs=33.89m Cjo=25.89p M=0.44 Vj=0.3245 BV=1000 IBV=5u TT=5.7u)
.model 1N5819 D(Is=31.7u N=1.373 Rs=51m Cjo=110p M=0.35 Vj=0.35 BV=40 IBV=1m)
.model 1N750 D(Is=880.5E-18 Rs=0.25 N=1 Cjo=175p M=0.5516 Vj=0.75 BV=4.7 IBV=20.245m)
.model 1N4733 D(Is=1n Rs=1 N=1.2 Cjo=140p M=0.4 Vj=0.75 BV=5.1 IBV=49m)
.model LED_ROJO D(Is=1e-19 N=1.8 Rs=2 Cjo=20p BV=5 IBV=10u)
""",
    "cmp/standard.bjt": """\
* Bipolares genéricos (CrossSpice, librería básica; parámetros típicos publicados).
.model 2N3904 NPN(Is=6.734f Xti=3 Eg=1.11 Vaf=74.03 Bf=416.4 Ne=1.259 Ise=6.734f Ikf=66.78m Xtb=1.5 Br=.7371 Nc=2 Isc=0 Ikr=0 Rc=1 Cjc=3.638p Mjc=.3085 Vjc=.75 Fc=.5 Cje=4.493p Mje=.2593 Vje=.75 Tr=239.5n Tf=301.2p Itf=.4 Vtf=4 Xtf=2 Rb=10)
.model 2N3906 PNP(Is=1.41f Xti=3 Eg=1.11 Vaf=18.7 Bf=180.7 Ne=1.5 Ise=0 Ikf=80m Xtb=1.5 Br=4.977 Nc=2 Isc=0 Ikr=0 Rc=2.5 Cjc=9.728p Mjc=.5776 Vjc=.75 Fc=.5 Cje=8.063p Mje=.3677 Vje=.75 Tr=33.42n Tf=179.3p Itf=.4 Vtf=4 Xtf=6 Rb=10)
.model 2N2222 NPN(Is=14.34f Xti=3 Eg=1.11 Vaf=74.03 Bf=255.9 Ne=1.307 Ise=14.34f Ikf=.2847 Xtb=1.5 Br=6.092 Nc=2 Isc=0 Ikr=0 Rc=1 Cjc=7.306p Mjc=.3416 Vjc=.75 Fc=.5 Cje=22.01p Mje=.377 Vje=.75 Tr=46.91n Tf=411.1p Itf=.6 Vtf=1.7 Xtf=3 Rb=10)
.model BC547B NPN(Is=7.049f Bf=374.7 Vaf=62.79 Ikf=82.17m Ise=68f Ne=1.576 Br=1 Nr=1 Rb=10 Rc=1 Cje=11.5p Mje=.3333 Vje=.75 Cjc=5.25p Mjc=.3333 Vjc=.75 Tf=410p Tr=10n)
.model BC557B PNP(Is=10.27f Bf=287.6 Vaf=45.08 Ikf=99.8m Ise=9.7f Ne=1.47 Br=7 Rb=10 Rc=1 Cje=15p Mje=.3333 Vje=.75 Cjc=10p Mjc=.3333 Vjc=.75 Tf=640p Tr=10n)
""",
    "cmp/standard.mos": """\
* MOSFET genéricos (CrossSpice, librería básica; valores aproximados).
.model 2N7000 VDMOS(Rg=3 Vto=1.6 Rd=0 Rs=.75 Rb=.14 Kp=.17 mtriode=1.25 Cgdmax=80p Cgdmin=12p Cgs=50p Cjo=50p Is=4n)
.model BS250 VDMOS(pchan Rg=3 Vto=-2.1 Rd=0 Rs=1 Rb=.2 Kp=.15 mtriode=1.2 Cgdmax=90p Cgdmin=15p Cgs=60p Cjo=60p Is=4n)
.model IRF540 VDMOS(Rg=3 Vto=3.5 Rd=10m Rs=15m Rb=20m Kp=25 lambda=0.01 Cgdmax=1.4n Cgdmin=100p Cgs=1.6n Cjo=1n Is=40p)
""",
}


def main():
    written = []
    for name, text in SYMBOLS.items():
        FILES[f"sym/{name}.asy"] = text
    for rel, text in FILES.items():
        p = os.path.join(ROOT, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(rel)
    with open(os.path.join(ROOT, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(sorted(written), fh, indent=1)
    print(len(written), "archivos")


if __name__ == "__main__":
    main()
