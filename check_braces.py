import re
c = open("extension/content.js").read()
print("Open:", c.count("{"), "Close:", c.count("}"), "Diff:", c.count("{")-c.count("}"))
print("Paren open:", c.count("("), "Paren close:", c.count(")"))
print("Bracket open:", c.count("["), "Bracket close:", c.count("]"))
