# Mission : trois chantiers sur vodloop v2, chaine nanatty247

Tu reprends une chaine de rediffusion Kick qui tourne 24/7 en v2 (bascule du
2026-09-17). Elle marche. Ton travail est de livrer les trois chantiers de la
section 5 sans jamais couper le fil.

Le PC de l'operateur peut etre eteint. Tout doit tenir sans lui.

`PROMPT.md` decrit la v1 (bin/, prep, feeder, collector, medic, janitor) qui est
arretee depuis la bascule. Le present fichier la remplace pour nanatty247.

---

## 1. Avant de toucher a quoi que ce soit

Dans cet ordre, et ne saute pas le troisieme :

1. La fin de `HANDOFF.md`, sections v2 : ce qui a ete garde de la v1 et pourquoi,
   ce qui a disparu, ce que la premiere heure a appris.
2. Les docstrings de `v2/oracle/*.py`. Chacune porte la mesure qui a decide du
   code en dessous. Elles sont la vraie documentation de la v2.
3. `docs/probes-that-lie.md` : les mesures qui ont donne une reponse fausse avec
   assurance. Une sonde qui echoue coute une minute, une sonde qui ment coute une
   heure parce qu'on part chercher ailleurs.
4. `docs/failures.md` : chaque panne datee, avec la mesure qui a tranche.
5. `README.md` pour la regle qui gouverne tout : on n'encode jamais.

---

## 2. Acces

    Oracle   ssh -i ~/.ssh/ssh-key-2026-05-07.key ubuntu@89.168.60.67
             code /home/ubuntu/v2/bin, chaine /home/ubuntu/v2/nanatty247
    Claw     wsl ssh -i /home/kil/.ssh/claw_key root@192.168.1.59
             MSYS_NO_PATHCONV=1 obligatoire depuis Git Bash
             le tableau : /root/v2/hangar, script /usr/bin/hangar, cron 5 min
    Depot    C:\Users\kil\Downloads\02 - Projects\vodloop
             branche nanatty247-channel, distant Pkkls/vodloop (prive)

Oracle ne peut pas joindre le Claw (NAT) : c'est toujours le Claw qui demande.
YouTube refuse l'adresse d'Oracle, donc le Claw est le seul chemin de
telechargement, y compris pour les shorts du chantier 5.3.

---

## 3. Methode, non negociable

**Mesure avant d'expliquer.** Une cause a laquelle tu as raisonne est une
supposition bien redigee. Chaque affirmation que tu rends a une commande derriere
elle.

**Un controle a besoin d'un temoin.** Avant de croire une mesure, reponds a deux
questions : qu'afficherait cette commande si la chose testee etait cassee, et
qu'afficherait-elle si la commande elle-meme etait cassee ? Si les deux reponses
sont identiques, la sonde ne mesure rien.

**Rien n'est corrige tant que ca n'a pas ete vu marcher dans le systeme qui
tourne.** Un test vert prouve le code, pas la chaine. Un fichier deploye ne
repare pas un processus deja lance avec l'ancien code en memoire.

**Chaque changement arrive avec son controle et son commit.** Anglais dans le
depot, aucun nom de chaine ou de createur en dur, jamais d'emoji.

---

## 4. Regles de securite de diffusion

Chacune a ete payee une fois.

- **Le pousseur ne redemarre jamais** pour reparer quelque chose en amont. Un
  seul ffmpeg tient la session RTMP ; tout au-dessus est concu pour qu'il ne
  bouge pas. `feed.py` peut redemarrer librement, lui non.
- **Kick fige son echelle sur la premiere image de la session.** La session est
  ouverte sur `filler.ts`, construit a MAXH lignes et 60 i/s. Tout morceau
  au-dessus de ce profil coupe le direct. C'est la contrainte qui gouverne
  entierement le chantier 5.3.
- **Rien de pose a la main dans `chunks/`.** `cut.py` en est proprietaire et le
  feeder supprime ce qu'il a envoye.
- **Le plancher disque rend toutes les autres reparations inutiles.** Sous
  `FLOOR_GB`, rien n'entre.
- **Deploiement : un fichier a la fois** (un `scp` multi-source a deja renvoye 0
  sans rien copier), empreintes comparees des deux cotes avec
  `tr -d "\r" | md5sum`, puis redemarrage de l'unite concernee, jamais du pousseur.
- **Ecrire un correctif par fichier**, jamais par heredoc a travers ssh : les
  couches de quoting mangent un niveau d'antislash.
- **Le Claw a un seul coeur et 211 Mo.** Grepper un gros journal pendant qu'il
  telecharge affame son sshd. Lire `board.json` depuis Oracle plutot que de le
  solliciter. Pas de python3 sur le Claw pendant un telechargement.

---

## 5. Les trois chantiers

### 5.1 Le chat des spectateurs sur Telegram

**Demande de l'operateur** : le log du chat de la chaine arrive sur le bot
Telegram, et une ligne tapee sur Telegram ressort dans le chat Kick.

**Etat mesure au 2026-09-21** : c'est ecrit, **non commite, non deploye, jamais
vu tourner**. `git status` sur la branche `nanatty247-channel` montre
`v2/oracle/bot.py` et `tests/test_v2.py` modifies, 250 lignes ajoutees.
Le code en place :

    bot.py:239-307   relay() empile les lignes, relay_loop() les envoie par
                     paquets toutes les TG_RELAY_SECONDS (15 s par defaut),
                     tg_loop() lit getUpdates, tg_reply() ressort sur Kick
    bot.py:1181      chaque chat.message.sent est relaye "pseudo: texte"
    bot.py:233       chaque reponse du bot est relayee "[bot] texte"
    chan.py:310      telegram() prefixe LABEL et coupe a 3900 caracteres

Deux choix deja faits, a ne pas defaire sans nouvel element : le lot de 15 s
existe parce que Telegram accepte environ vingt messages par minute dans une
salle et jette le reste, et seule la chaine portant `TG_POLL=1` lit le long poll
parce que Telegram ne donne `getUpdates` qu'a un seul lecteur.

**Ce qui reste a faire** :

1. Faire tourner `tests/test_v2.py` en entier et le rendre vert.
2. Deployer `bot.py` sur Oracle, un fichier, empreinte comparee, unite du bot
   redemarree, **pas** le pousseur.
3. Verifier sur le systeme qui tourne : une ligne ecrite dans le chat Kick
   apparait dans la salle Telegram en moins de TG_RELAY_SECONDS + 5 s, et une
   ligne tapee sur Telegram ressort dans le chat Kick sous le nom de la chaine.
4. **Temoin obligatoire** : avec `TG_TOKEN` vide, rien n'est empile ni envoye,
   et le bot repond quand meme sur Kick. Et une ligne envoyee depuis une **autre**
   salle Telegram ne doit rien ecrire sur Kick (`tg_reply` compare l'id de salle).
   Un controle qui n'a jamais vu le cas refuse ne prouve rien.
5. Regarder ce que couvrent les 60 lignes gardees en memoire (`del _relay[:-60]`)
   quand Telegram est injoignable plusieurs minutes : dire si c'est ce qu'on veut
   ou si le log doit survivre a une coupure plus longue, avec le chiffre.

### 5.2 Beaucoup plus de fetch

**Demande de l'operateur** : la chaine doit recevoir nettement plus de matiere.

**Ce qui borne aujourd'hui, avec les chiffres et leur origine** :

    hangar (Claw)   GAP_MIN=90 entre deux telechargements, module par la reserve :
                    < 4 h -> 15 min, < 8 h -> 35, < 16 h -> 90, au-dela -> 180
                    DAY_MB=16000    plafond glissant sur 24 h
                    HOUR_MB=2500    plafond de debit, c'est lui le vrai garde-fou
                    FAIL_GAP_MIN=180, budget divise par 2 par niveau de mur,
                    THROTTLE_HOLD_MIN=360, jusqu'a 3 niveaux
                    RECENT=120      profondeur du tirage dans le catalogue
    supply (Oracle) WINDOW_HOURS=16 la reserve visee, BUDGET_GB=28 la part disque,
                    MAX_FILE_GB=10, MIN_SECONDS=3600, MAX_SECONDS=43200
    le mur         28 Go en dix heures ont fait refuser l'adresse le 2026-09-14,
                   soit 2,8 Go/h. C'est un debit, pas un total.
    la matiere     les longs VOD IRL de ce vivier sortent a 0,31 Go/h en 720p30

Ces chiffres disent une chose qu'il faut verifier avant de toucher a quoi que ce
soit : a 0,31 Go/h, 16 Go par 24 h representent une cinquantaine d'heures de
video par jour pour une chaine qui en consomme 24. **Si la reserve fond quand
meme, le plafond quotidien n'est pas ce qui manque**, et augmenter DAY_MB ne
donnera rien. Les suspects, dans l'ordre ou il faut les mesurer :

1. **Le lien montant du Claw.** Un coeur RISC-V, mesure historique ~46 Mo en
   60 s, soit environ 2,7 Go/h en pointe theorique et moins en pratique puisque
   le meme coeur telecharge. Mesurer le debit reel d'une livraison
   (`fetched.tsv`, horodatage et taille) avant de parler de rythme.
2. **Le temps passe verrouille.** Un telechargement tient le verrou pres d'une
   heure. Compter, sur 24 h de `hangar.log`, combien de ticks de cron sont sortis
   sur verrou et combien de minutes le tableau a passe a ne rien faire.
3. **Le throttle.** Compter les entrees "YouTube a refuse" sur 7 jours : si le
   budget passe sa vie divise par deux ou quatre, le probleme est la forme des
   requetes, pas le plafond.
4. **Le tirage.** `RECENT=120` et les refus definitifs (`rejected.tsv`) peuvent
   assecher la liste de candidats. Compter les lignes de `candidates.tsv`
   reellement tirables.

Seulement ensuite, et chaque levier avec sa mesure d'arrivee :

- rapprocher les paliers de `gap` quand la reserve est basse, sans jamais depasser
  `HOUR_MB` ;
- monter `DAY_MB` si et seulement si le point 1 montre que le lien suit ;
- **la deuxieme ligne d'approvisionnement** : `kickfetch.py` tire les VOD de Kick
  directement sur Oracle, sans passer par le tableau ni par son budget, la CDN de
  Kick servant Oracle a 33 Mbps. Elle est **coupee sur nanatty247** (`NO_KICK=1`,
  decision de l'operateur, commit d4f02b8). C'est le plus gros levier disponible
  et il ne coute rien au Claw : **demander a l'operateur avant de la rallumer**,
  ne pas la rallumer de ta propre initiative.

Ce qui ne se negocie pas : le mur est un debit, on ne le retrouve pas en rafale.
En cas de refus par la source, vider la file du Claw **avant** tout diagnostic,
chaque item reessayant plusieurs fois et entretenant le signalement.

### 5.3 Des shorts pendant qu'une VOD se telecharge

**Demande de l'operateur** : quand il n'y a pas de VOD disponible, la chaine
affiche des shorts, une vingtaine, recuperes d'avance, diffuses pendant le
telechargement d'une VOD, **sans que la diffusion soit interrompue**.

**Ce qui se passe aujourd'hui** : sans morceau dans `chunks/`, `feed.py` envoie
`filler.ts`, un clip de 20 s a MAXH lignes et 60 i/s qui affiche
"vod loading...", en boucle. C'est le seul encodage de la v2, fait une fois par
`install.sh`.

**La contrainte qui decide de tout le chantier** : la session RTMP est ouverte
sur `filler.ts` et Kick fige son echelle dessus. Un short YouTube est vertical,
1080x1920, souvent 30 i/s, dans un codec que le fil ne copie pas forcement. Tel
quel il coupe le direct. Il faut donc le remettre exactement dans le profil du
clip d'attente, et remettre une image verticale dans un cadre 16:9 est un
encodage. C'est precisement ce que la v2 interdit sur la matiere longue, a
0,06x le temps reel sur les deux vCPU qui alimentent deja la diffusion.

**La forme recommandee, parce qu'elle paye l'encodage une fois** : une bobine.
Vingt shorts concatenes en un seul encodage, aux parametres **identiques** a ceux
de `install.sh` (`-c:v libx264 -pix_fmt yuv420p -g 120 -keyint_min 120
-sc_threshold 0 -c:a aac -b:a 160k -ar 44100 -ac 2 -f mpegts`, taille et 60 i/s
selon MAXH), le vertical mis a l'echelle et complete par
`scale=...:force_original_aspect_ratio=decrease,pad`. La bobine est decoupee aux
memes 5 minutes que le reste et deposee dans un repertoire d'attente que
`feed.py` lit **a la place** de boucler le clip de 20 s, la ou il fait
aujourd'hui `source = chunks[0] if chunks else chan.FILLER`.

Ce que cette forme garantit, et c'est pour ca qu'elle est proposee :

- **rien n'est interrompu** : le profil est celui de la session, donc aucune
  reouverture ; l'arrivee d'une vraie VOD reprend la main a la fin du morceau en
  cours, exactement comme une rotation normale ;
- l'encodage a lieu une fois, hors ligne, `nice`, et pas a chaque diffusion ;
- `chan.FILLER` reste le clip qui **ouvre** la session : ne pas le remplacer par
  un short, l'ouverture doit rester un fichier dont le profil est certain.

Les points a trancher par la mesure, pas par le raisonnement :

1. **Le cout d'encodage reel** de vingt shorts sur Oracle, `nice -n 19`, pendant
   que la chaine diffuse : temps total, et effet mesure sur le fil (aucun
   `NRestarts` du pousseur, aucun passage sur le clip d'attente). Si ca touche le
   fil, l'encodage descend sur une autre machine ou se fait par lots de deux.
2. **D'ou viennent les shorts.** YouTube refuse l'adresse d'Oracle : ils passent
   par le Claw comme le reste. Ils sont en dessous de `MIN_SECONDS=3600`, donc ils
   **ne peuvent pas** transiter par `candidates.tsv` ni par `want.json` sans
   casser l'invariant de duree. Chemin separe, budget separe, et ils ne comptent
   pas dans la reserve d'heures inedites.
3. **La rotation.** Une bobine rejouee en boucle pendant deux heures est pire
   qu'un clip d'attente. Dire au bout de combien de temps elle se repete et ce
   qui se passe quand elle est epuisee.
4. **Le signal.** Le clip actuel dit "vod loading...". Quand la bobine passe,
   `watch.py` et `!now` doivent continuer a distinguer "la chaine attend de la
   matiere" de "la chaine diffuse". Ne pas faire disparaitre l'aveu : un ecran
   agreable qui cache une panne d'approvisionnement coute plus cher qu'un ecran
   moche qui la montre.

Questions ouvertes a poser a l'operateur **avant** de coder : quelle source pour
les shorts (ceux de la chaine elle-meme ou un choix plus large), et est-ce que la
bobine remplace le clip d'attente dans tous les cas ou seulement quand un
telechargement est reellement en vol.

---

## 6. Ce que tu rends

Pour chaque chantier : la mesure de depart, le changement, la mesure d'arrivee,
le temoin qui prouve que la mesure sait detecter l'echec, un commit, et une
entree dans `docs/failures.md` si tu as decouvert une panne.

Des chiffres, jamais des estimations. "Je n'ai pas mesure" est une reponse
acceptable, "ca devrait marcher" ne l'est pas.

Si une action risque de couper la diffusion, dis-le **avant**, avec le cout
attendu, pas apres.

---

## 7. Tranche, ne pas rouvrir sans nouvel element

- **On n'encode pas la matiere longue.** 0,06x le temps reel sur une machine qui
  consomme a 1x pour toujours. La seule exception reste le clip d'attente, et la
  bobine de shorts si la mesure du 5.3 point 1 la valide.
- **Le pousseur ne redemarre jamais**, et la session s'ouvre sur le clip.
- **Une heure passee a l'antenne ne repasse pas** tant qu'il reste de l'inedit
  sur le disque.
- **Plus de source Kick sur cette chaine** (`NO_KICK=1`) : decision de
  l'operateur, a rouvrir avec lui et pas autrement.
- **Le tableau est paye au debit, pas au total.** Le mur est une vitesse.
