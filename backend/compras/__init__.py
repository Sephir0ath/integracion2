"""Orquestador de la saga de compra de entradas.

El servicio `compras` es el unico que conoce la secuencia completa de la
compra: reserva las butacas, cobra y emite las entradas. Cada paso tiene
su compensacion y, cuando un paso falla, se compensan **en orden inverso**
los pasos que ya estaban confirmados.
"""
