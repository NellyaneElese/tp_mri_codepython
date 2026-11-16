"""
Application de prise de rendez-vous - version Python (MRI TP2)
Outils : Flask (routes), Waitress (serveur), PyMySQL (connexion a MySQL)

Installation, une seule fois :   pip install -r requirements.txt
Lancement :                      python app.py
Adresse :                        http://localhost:8001

Reglages MySQL : 127.0.0.1, port 3306, utilisateur root, sans mot de passe, base rdv.
Si MySQL utilise un autre port (exemple 3307), taper avant le lancement :  set DB_PORT=3307
"""

import logging
import os
import queue
import re
import socket
import sys
from contextlib import contextmanager
from datetime import datetime

import pymysql
from flask import Flask, jsonify, request
from pymysql.constants import CLIENT, FIELD_TYPE
from waitress import serve

PORT = 8001
TAILLE_POOL = 25  # nombre maximal de connexions a MySQL, comme dans les autres versions
STATUTS = ("en_attente", "confirme", "annule")
COLONNES = "id, nom_client, telephone, service, date, statut"

MODELE_DATE_HEURE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
MODELE_JOUR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MODELE_ENTIER = re.compile(r"^[+-]?\d+$")

app = Flask(__name__)
app.json.ensure_ascii = False  # les accents sont envoyes tels quels, en UTF-8
app.json.sort_keys = False     # les champs gardent l'ordre id, nom_client, ..., statut


# ---------- Connexions a MySQL ----------

# MySQL renvoie la date sous la forme "2026-12-01 09:00:00" : on la garde telle quelle.
CONVERSIONS = pymysql.converters.conversions.copy()
CONVERSIONS.pop(FIELD_TYPE.DATETIME, None)


def ouvrir_connexion():
    return pymysql.connect(
        host="127.0.0.1",
        port=int(os.environ.get("DB_PORT", "3306")),
        user="root",
        password="",
        database="rdv",
        charset="utf8mb4",
        autocommit=True,
        conv=CONVERSIONS,
        cursorclass=pymysql.cursors.DictCursor,
        # Une mise a jour qui ne change rien compte quand meme comme "ligne trouvee"
        client_flag=CLIENT.FOUND_ROWS,
    )


# Le pool : 25 places. Une place vide (None) est remplie a la premiere utilisation.
pool = queue.LifoQueue()
for _ in range(TAILLE_POOL):
    pool.put(None)


@contextmanager
def connexion():
    """Prete une connexion du pool, puis la remet a sa place."""
    c = pool.get()
    try:
        if c is None:
            c = ouvrir_connexion()
        yield c
    except Exception:
        # En cas d'erreur, la connexion est fermee : une neuve sera ouverte au besoin.
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        pool.put(None)
        raise
    else:
        pool.put(c)


# ---------- Outils ----------

def erreur(code, message):
    return jsonify({"erreur": message}), code


def date_valide(texte, modele, format_date):
    """Vrai si le texte a exactement la forme attendue et designe une vraie date."""
    if not modele.match(texte):
        return False
    try:
        datetime.strptime(texte, format_date)
        return True
    except ValueError:
        return False


@app.errorhandler(pymysql.MySQLError)
def erreur_base(_):
    return erreur(500, "erreur serveur")


# ---------- Les quatre fonctions ----------

# 1. Prendre un rendez-vous : POST /rendez-vous
@app.post("/rendez-vous")
def creer():
    donnees = request.get_json(force=True, silent=True)
    if not isinstance(donnees, dict):
        return erreur(400, "JSON invalide")
    nom, telephone, service, date = (donnees.get(c) for c in ("nom_client", "telephone", "service", "date"))
    for valeur in (nom, telephone, service, date):
        if not isinstance(valeur, str) or valeur == "":
            return erreur(400, "champs requis : nom_client, telephone, service, date")
    if not date_valide(date, MODELE_DATE_HEURE, "%Y-%m-%d %H:%M:%S"):
        return erreur(400, "date invalide (format : 2026-12-01 09:00:00)")

    with connexion() as c, c.cursor() as curseur:
        curseur.execute(
            "INSERT INTO rendez_vous (nom_client, telephone, service, date, statut) VALUES (%s, %s, %s, %s, 'en_attente')",
            (nom, telephone, service, date))
        identifiant = curseur.lastrowid
    return jsonify({"id": identifiant, "nom_client": nom, "telephone": telephone,
                    "service": service, "date": date, "statut": "en_attente"}), 201


# 2 et 3. Lister : GET /rendez-vous   ou   GET /rendez-vous?date=2026-12-01
@app.get("/rendez-vous")
def lister():
    requete = "SELECT " + COLONNES + " FROM rendez_vous"
    parametres = ()
    jour = request.args.get("date", "")
    if jour != "":
        if not date_valide(jour, MODELE_JOUR, "%Y-%m-%d"):
            return erreur(400, "date invalide (format : 2026-12-01)")
        requete += " WHERE DATE(date) = %s"
        parametres = (jour,)
    requete += " ORDER BY id ASC"

    with connexion() as c, c.cursor() as curseur:
        curseur.execute(requete, parametres)
        liste = curseur.fetchall()
    return jsonify(list(liste)), 200


# 4. Changer le statut : PUT /rendez-vous/{id}/statut
@app.put("/rendez-vous/<identifiant>/statut")
def changer_statut(identifiant):
    if not MODELE_ENTIER.match(identifiant):
        return erreur(400, "id invalide")
    identifiant = int(identifiant)
    donnees = request.get_json(force=True, silent=True)
    if not isinstance(donnees, dict) or donnees.get("statut") not in STATUTS:
        return erreur(400, "statut invalide : en_attente, confirme ou annule")

    with connexion() as c, c.cursor() as curseur:
        curseur.execute("UPDATE rendez_vous SET statut = %s WHERE id = %s", (donnees["statut"], identifiant))
        if curseur.rowcount == 0:
            return erreur(404, "rendez-vous introuvable")
        curseur.execute("SELECT " + COLONNES + " FROM rendez_vous WHERE id = %s", (identifiant,))
        rendez_vous = curseur.fetchone()
    if rendez_vous is None:
        return erreur(500, "erreur serveur")
    return jsonify(rendez_vous), 200


if __name__ == "__main__":
    # Verification de la connexion avant de demarrer
    try:
        ouvrir_connexion().close()
    except pymysql.MySQLError as probleme:
        print("connexion MySQL impossible :", probleme)
        sys.exit(1)

    # Sous forte charge, Waitress ecrit un avertissement par demande en attente :
    # on les masque pour que l'affichage ne ralentisse pas les mesures.
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)

    print(f"API sur http://localhost:{PORT}")
    # Ecoute en IPv4 et en IPv6 quand c'est possible, pour que "localhost" reponde
    # sans delai sous Windows ; en IPv4 seulement si la machine n'a pas IPv6.
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        ecoute = f"*:{PORT}"
    except OSError:
        ecoute = f"0.0.0.0:{PORT}"
    serve(app, listen=ecoute, threads=TAILLE_POOL, connection_limit=1000)
