Version 4
SymbolType CELL
LINE Normal -32 32 32 64
LINE Normal 32 64 -32 96
LINE Normal -32 96 -32 32
LINE Normal -24 48 -12 48
LINE Normal -24 80 -12 80
LINE Normal -18 74 -18 86
WINDOW 0 0 32 Left 2
SYMATTR Prefix X
SYMATTR Description Amplificador operacional ideal de un polo (Aol, GBW). Requiere .lib opamp.sub
SYMATTR Value opamp
SYMATTR SpiceLine Aol=100K
SYMATTR SpiceLine2 GBW=10Meg
PIN -32 48 NONE 0
PINATTR PinName invin
PINATTR SpiceOrder 1
PIN -32 80 NONE 0
PINATTR PinName noninvin
PINATTR SpiceOrder 2
PIN 32 64 NONE 0
PINATTR PinName out
PINATTR SpiceOrder 3
