# gold_layman_v1.1 — human-readable index (AQ-03, PROVISIONAL / lawyer-pending)

Corpus snapshot: pythia 658 source_documents / 54414 provisions / 43129 practical_fact (12531 amount) (grounded 2026-07-02, re-pinned 2026-07-07, read-only)
Total items: 100  ·  ALL labels verified=false / source=draft (lawyer certifies later — gate #1)

## Class counts (v1.1 — every mandatory class ≥ 10)
- multi_obligation: 38
- zero_lexical: 22
- amount: 24
- must_refuse: 16

## Copy note
Human-readable copy destination (operator): `Polarius Project/Eval Sets/gold_layman_v1.INDEX.md` and the raw `gold_layman_v1.jsonl`.
Canonical source of truth is `engine/eval/gold_layman_v1.jsonl`.

## Items
### emp-first-hire-plzen-01  (multi_obligation → answer)
- Q: Mám malou restauraci v Plzni a chci poprvé v životě někoho zaměstnat na stálo. Co všechno musím udělat, než mi ten člověk nastoupí, a na koho se musím kde hlásit?
- gold ids: 262/2006 § 34, 262/2006 § 35, 262/2006 § 96, 262/2006 § 103, 187/2006 § 92, 589/1992 § 3, 592/1992 § 3, 586/1992 § 6, 285/2024 § 1
- clusters: pracovní_smlouva, evidence_pracovní_doby, bozp_vstupní_prohlídka, přihlášení_sociální_pojištění, přihlášení_zdravotní_pojištění, srážková_daň_ze_mzdy, minimální_mzda
- facts: 285/2024 § 1=«Koeficient pro výpočet minimální mzdy pr» ; 187/2006 § 92=«Úkoly zaměstnavatelů v pojištění plní v »

### emp-first-hire-eshop-02  (multi_obligation → answer)
- Q: Rozjíždím malý e-shop jako s.r.o. a beru prvního brigádníka na dohodu. Musím kvůli tomu něco hlásit na úřady a musím ho posílat na nějakou prohlídku?
- gold ids: 262/2006 § 74, 262/2006 § 75, 262/2006 § 77, 262/2006 § 103, 187/2006 § 92, 589/1992 § 3, 592/1992 § 3
- clusters: dohoda_mimo_pracovní_poměr, bozp_vstupní_prohlídka, přihlášení_sociální_pojištění, přihlášení_zdravotní_pojištění
- facts: 262/2006 § 75=«Dohoda o provedení práce»

### vat-turnover-register-03  (multi_obligation → answer)
- Q: Živím se jako řemeslník na živnost a poslední dobou mi hodně přibylo zakázek. Od kolika já musím začít účtovat DPH a do kdy se musím někam přihlásit?
- gold ids: 235/2004 § 6, 235/2004 § 94
- clusters: povinná_registrace_dph, lhůta_registrace
- facts: 235/2004 § 94=«Osoba podle § 6 odst. 1 je povinna podat»

### trade-license-start-04  (multi_obligation → answer)
- Q: Chci si otevřít malou kavárnu. Kde vůbec začnu papírově, abych to měl legálně, a co potřebuju vyřídit jako první?
- gold ids: 455/1991 § 10, 455/1991 § 45, 563/1991 § 4
- clusters: živnostenské_oprávnění, ohlášení_živnosti, účetnictví
- facts: 455/1991 § 45=«Fyzická nebo právnická osoba, která hodl»

### probation-length-05  (multi_obligation → answer)
- Q: Beru nového člověka a nejsem si jistý, jak dlouho ho můžu mít na zkoušku a jestli to musím psát do smlouvy dopředu.
- gold ids: 262/2006 § 35, 262/2006 § 34
- clusters: zkušební_doba, náležitosti_pracovní_smlouvy
- facts: 262/2006 § 35=«Zkušební dobu je možné sjednat nejpozděj»

### working-time-records-06  (multi_obligation → answer)
- Q: Musím jako zaměstnavatel někde zapisovat, kdy mí lidi přišli a odešli, nebo stačí, že jim vyplatím mzdu?
- gold ids: 262/2006 § 96, 262/2006 § 78
- clusters: evidence_pracovní_doby
- facts: 262/2006 § 96=«Zaměstnavatel je povinen vést u jednotli»

### annual-leave-newco-07  (multi_obligation → answer)
- Q: Kolik dní dovolené musím dát zaměstnanci za rok? Existuje nějaké zákonné minimum, nebo si to určím sám?
- gold ids: 262/2006 § 213
- clusters: dovolená
- facts: 262/2006 § 213=«Dovolená za kalendářní rok a její poměrn»

### consumer-complaint-08  (multi_obligation → answer)
- Q: Prodávám zboží v kamenném obchodě a přišel za mnou zákazník s reklamací. Musím ji přijmout hned a kde vlastně?
- gold ids: 634/1992 § 13, 634/1992 § 19
- clusters: reklamace_informační_povinnost, přijetí_reklamace
- facts: 634/1992 § 19=«je prodávající povinen přijmout reklamac»

### eshop-withdrawal-14d-09  (multi_obligation → answer)
- Q: Mám e-shop. Když si u mě někdo koupí věc přes internet, může ji vrátit bez udání důvodu, a do kdy?
- gold ids: 89/2012 § 1829, 89/2012 § 1831, 89/2012 § 1832
- clusters: odstoupení_distanční_smlouva, vrácení_plnění
- facts: 89/2012 § 1829=«Spotřebitel může odstoupit od smlouvy uz»

### remote-work-agreement-10  (multi_obligation → answer)
- Q: Chci nechat zaměstnance dělat z domova. Stačí, že se domluvíme ústně, nebo to musí být nějak papírově?
- gold ids: 262/2006 § 317
- clusters: práce_na_dálku
- facts: 262/2006 § 317=«Výkon práce na dálku je možný jen na zák»

### occ-health-check-11  (multi_obligation → answer)
- Q: Nový zaměstnanec mi za týden nastupuje. Musím ho poslat k nějakému doktorovi, než začne pracovat?
- gold ids: 262/2006 § 103, 373/2011 § 54
- clusters: bozp_vstupní_prohlídka, pracovnělékařské_služby
- facts: 262/2006 § 103=«Zaměstnavatel je povinen»

### accounting-obligation-12  (multi_obligation → answer)
- Q: Založil jsem si eseróčko. Musím hned od začátku vést nějaké účetnictví, nebo mi stačí daňová evidence jako OSVČ?
- gold ids: 563/1991 § 4
- clusters: účetnictví
- facts: 563/1991 § 4=«jsou povinny vést účetnictví ode dne své»

### min-wage-2026-13  (multi_obligation → answer)
- Q: Kolik nejmíň musím platit člověku za měsíc, aby to bylo podle zákona, když ho beru na plný úvazek v roce 2026?
- gold ids: 285/2024 § 1, 567/2006 § 2
- clusters: minimální_mzda
- facts: 285/2024 § 1=«Koeficient pro výpočet minimální mzdy pr»

### social-health-contrib-14  (multi_obligation → answer)
- Q: Když někoho zaměstnám, kolik za něj odvádím státu na pojištění a komu to vlastně platím?
- gold ids: 589/1992 § 3, 589/1992 § 5, 592/1992 § 3
- clusters: sociální_pojištění, zdravotní_pojištění
- facts: 589/1992 § 3=«Pojistné jsou povinni v rozsahu a za pod»

### termination-notice-15  (multi_obligation → answer)
- Q: Chci se rozejít se zaměstnancem, který mi nefunguje. Jak dlouho mu běží výpovědní lhůta a můžu ho vyhodit ze dne na den?
- gold ids: 262/2006 § 51, 262/2006 § 52
- clusters: výpovědní_doba, výpovědní_důvody

### hire-foreigner-16  (multi_obligation → answer)
- Q: Chci zaměstnat člověka z Ukrajiny. Co všechno si musím ohlídat, aby to nebylo načerno?
- gold ids: 435/2004 § 87, 435/2004 § 89
- clusters: zaměstnání_cizince, oznamovací_povinnost_úřad_práce

### colloq-cookies-17  (zero_lexical → answer)
- Q: Na svém webu mám ty cookies lišty. Musím mít od lidí souhlas, než jim tam ty cookies nahraju?
- gold ids: 127/2005 § 89
- clusters: cookies_souhlas

### colloq-svarcsystem-18  (zero_lexical → answer)
- Q: Slyšel jsem o švarcsystému. Když mi člověk fakturuje na IČO, ale dělá u mě jako klasický zaměstnanec, je to problém?
- gold ids: 435/2004 § 5, 435/2004 § 140
- clusters: nelegální_práce, zastřený_pracovní_poměr
- facts: 435/2004 § 5=«Vymezení některých pojmů»

### colloq-newsletter-spam-19  (zero_lexical → answer)
- Q: Chci lidem rozesílat newsletter s nabídkami. Můžu prostě vzít e-maily zákazníků a posílat jim to, nebo potřebuju souhlas?
- gold ids: 480/2004 § 7
- clusters: obchodní_sdělení_souhlas
- facts: 480/2004 § 7=«Obchodní sdělení lze šířit elektronickým»

### colloq-marketplace-20  (zero_lexical → answer)
- Q: Prodávám svoje výrobky přes marketplace jako Kaufland Marketplace. Jsem z pohledu zákona pořád normální prodejce se všemi povinnostmi?
- gold ids: 634/1992 § 13
- clusters: postavení_prodávajícího

### colloq-dropshipping-21  (zero_lexical → answer)
- Q: Dělám dropshipping - zboží mi posílá dodavatel z Číny rovnou zákazníkovi. Kdo je z pohledu zákona prodejce a kdo řeší reklamace?
- gold ids: 634/1992 § 13, 634/1992 § 19
- clusters: postavení_prodávajícího, reklamace

### colloq-influencer-22  (zero_lexical → answer)
- Q: Jsem influencer a dostávám zaplaceno za reklamu na Instagramu. Musím to nějak označovat, aby to bylo podle zákona?
- gold ids: 634/1992 § 5
- clusters: skrytá_reklama_nekalá_praktika

### colloq-homeoffice-23  (zero_lexical → answer)
- Q: Dávám zaměstnancům home office. Musím jim proplácet nějaké náklady za to, že dělají z domova?
- gold ids: 262/2006 § 317
- clusters: práce_na_dálku_náhrady
- facts: 262/2006 § 317=«Práce na dálku»

### colloq-gdpr-24  (zero_lexical → answer)
- Q: Furt slyším o GDPR. Když mi u někoho unikne databáze zákazníků, musím to někam nahlásit a do kdy?
- gold ids: 110/2019 § 41, 110/2019 § 42
- clusters: ohlášení_porušení_zabezpečení, oznámení_subjektu_údajů
- facts: 110/2019 § 41=«Ohlašování porušení zabezpečení osobních»

### colloq-blackwork-25  (zero_lexical → answer)
- Q: Kámoš mi občas vypomůže v dílně a dám mu něco na ruku bez papírů. Můžu se kvůli tomu dostat do problémů?
- gold ids: 435/2004 § 5, 435/2004 § 140
- clusters: nelegální_práce, sankce

### colloq-cashonly-26  (zero_lexical → answer)
- Q: Beru v hospodě jenom cash a nedávám účtenky. Musím ze zákona dávat lidem účtenku?
- gold ids: 634/1992 § 16
- clusters: doklad_o_zakoupení

### amount-minwage-monthly-27  (amount → answer)
- Q: Jaká je teď nejnižší možná mzda za měsíc, kterou musím dát zaměstnanci na plný úvazek?
- gold ids: 285/2024 § 1
- clusters: minimální_mzda
- facts: 285/2024 § 1=«Koeficient pro výpočet minimální mzdy pr»

### amount-stravne-domestic-28  (amount → answer)
- Q: Posílám zaměstnance na služebku na celý den. Kolik mu musím dát nejmíň na jídlo (stravné)?
- gold ids: 475/2024 § 2
- clusters: stravné
- facts: 475/2024 § 2=«148 Kč, trvá-li pracovní cesta 5 až 12 h»

### amount-stravne-bands-29  (amount → answer)
- Q: Můj zaměstnanec byl na cestě 15 hodin. Kolik korun mu náleží jako stravné?
- gold ids: 475/2024 § 2
- clusters: stravné
- facts: 475/2024 § 2=«225 Kč, trvá-li pracovní cesta déle než »

### amount-mileage-30  (amount → answer)
- Q: Zaměstnanec jel služebně vlastním autem. Kolik korun za kilometr mu musím proplatit?
- gold ids: 475/2024 § 1
- clusters: základní_náhrada_km
- facts: 475/2024 § 1=«osobních silničních motorových vozidel 5»

### amount-vat-deadline-31  (amount → answer)
- Q: Překročil jsem obrat pro DPH. Kolik dní mám na to, abych podal přihlášku, než dostanu pokutu?
- gold ids: 235/2004 § 94
- clusters: lhůta_registrace_dph
- facts: 235/2004 § 94=«je povinna podat přihlášku k registraci »

### amount-dpp-hours-32  (amount → answer)
- Q: Kolik hodin ročně smí u mě odpracovat člověk na dohodu o provedení práce?
- gold ids: 262/2006 § 75
- clusters: rozsah_dpp
- facts: 262/2006 § 75=«Rozsah práce vykonávané na základě dohod»

### amount-minwage-coef-2026-33  (amount → answer)
- Q: Podle čeho se pro rok 2026 počítá minimální mzda a jaké je to číslo v tom vzorci?
- gold ids: 285/2024 § 1
- clusters: minimální_mzda_koeficient
- facts: 285/2024 § 1=«Koeficient pro výpočet minimální mzdy pr»

### amount-stravne-long-34  (amount → answer)
- Q: Zaměstnanec byl na cestě přes 18 hodin. Kolik mu musím dát na stravné za ten den?
- gold ids: 475/2024 § 2
- clusters: stravné
- facts: 475/2024 § 2=«353 Kč, trvá-»

### refuse-offcorpus-us-35  (must_refuse → refuse)
- Q: Kolik zaplatím na federal income tax v USA, když mám s.r.o. v Delaware?

### refuse-nonlegal-recipe-36  (must_refuse → refuse)
- Q: Jaký je nejlepší recept na svíčkovou pro naši restauraci?

### refuse-falsepremise-eet-37  (must_refuse → refuse)
- Q: Do kdy musím podle zákona o EET zaregistrovat každou tržbu online na finanční správu?

### refuse-falsepremise-3employees-38  (must_refuse → refuse)
- Q: Je pravda, že když mám míň než 3 zaměstnance, tak vůbec nemusím platit sociální pojištění?

### refuse-medical-39  (must_refuse → refuse)
- Q: Bolí mě záda z práce u počítače, jaké prášky si mám vzít?

### refuse-offcorpus-germany-40  (must_refuse → refuse)
- Q: Jaká je minimální mzda v Německu pro moji pobočku v Mnichově?

### refuse-cryptolegal-41  (must_refuse → refuse)
- Q: Poraď mi, jak legálně neplatit daně z krypta úplně vůbec.

### refuse-personalopinion-42  (must_refuse → refuse)
- Q: Myslíš, že bych měl radši propustit Pepu nebo Honzu? Kdo se ti zdá lepší?

### refuse-futurelaw-43  (must_refuse → refuse)
- Q: Jaká bude minimální mzda v roce 2035?

### refuse-otherjurisdiction-slovak-44  (must_refuse → refuse)
- Q: Podle slovenského Zákonníka práce - jak dlouhá je výpovědní doba na Slovensku?

### clarify-vague-business-45  (multi_obligation → clarify)
- Q: Chci začít podnikat. Co musím udělat?
- gold ids: 455/1991 § 45, 455/1991 § 10
- clusters: živnostenské_oprávnění

### clarify-vague-tax-46  (amount → clarify)
- Q: Kolik zaplatím na daních?

### amount-weekly-hours-47  (amount → answer)
- Q: Kolik hodin týdně smí u mě zaměstnanec normálně odpracovat, aniž by to byly přesčasy?
- gold ids: 262/2006 § 79
- clusters: stanovená_pracovní_doba
- facts: 262/2006 § 79=«Délka stanovené týdenní pracovní doby či»

### amount-tax-rate-48  (amount → answer)
- Q: Kolik procent daně z příjmu zaplatím jako fyzická osoba z toho, co vydělám?
- gold ids: 586/1992 § 16
- clusters: sazba_daně_z_příjmu
- facts: 586/1992 § 16=«Sazba daně činí   a) 15 % pro část zákla»

### amount-maternity-49  (amount → answer)
- Q: Jak dlouho má moje zaměstnankyně nárok na mateřskou, když čeká dítě?
- gold ids: 262/2006 § 195
- clusters: mateřská_dovolená
- facts: 262/2006 § 195=«přísluší zaměstnankyni mateřská dovolená»

### amount-break-50  (amount → answer)
- Q: Po kolika hodinách práce musím dát zaměstnanci pauzu na jídlo a jak je dlouhá?
- gold ids: 262/2006 § 88
- clusters: přestávka_v_práci
- facts: 262/2006 § 88=«nejdéle po 6 hodinách nepřetržité práce »

### amount-weekly-rest-51  (amount → answer)
- Q: Kolik hodin v kuse musí mít zaměstnanec volno během týdne?
- gold ids: 262/2006 § 92
- clusters: nepřetržitý_odpočinek_v_týdnu
- facts: 262/2006 § 92=«nepřetržitý odpočinek v trvání alespoň 2»

### amount-tax-deadline-52  (amount → answer)
- Q: Do kdy musím podat daňové přiznání za rok?
- gold ids: 280/2009 § 136
- clusters: lhůta_daňové_přiznání
- facts: 280/2009 § 136=«Daňové přiznání u daní vyměřovaných za z»

### emp-immediate-termination-53  (multi_obligation → answer)
- Q: Zaměstnanec mě okradl. Můžu ho vyhodit na hodinu, nebo mu i tak běží výpovědní doba?
- gold ids: 262/2006 § 55, 262/2006 § 48
- clusters: okamžité_zrušení_pracovního_poměru
- facts: 262/2006 § 55=«Okamžité zrušení pracovního poměru zaměs»

### emp-quit-immediate-54  (multi_obligation → answer)
- Q: Zaměstnanec mi 2 měsíce nezaplatil... teda já jemu 2 měsíce nedal mzdu. Může ode mě odejít okamžitě?
- gold ids: 262/2006 § 56
- clusters: okamžité_zrušení_zaměstnancem
- facts: 262/2006 § 56=«Okamžité zrušení pracovního poměru zaměs»

### sro-formation-55  (multi_obligation → answer)
- Q: Jak si založím eseróčko? Co musíme sepsat a co v tom musí být napsané?
- gold ids: 90/2012 § 8, 90/2012 § 146, 90/2012 § 132
- clusters: založení_sro, obsah_společenské_smlouvy
- facts: 90/2012 § 8=«Obchodní korporace se zakládá společensk»

### vat-invoice-duty-56  (multi_obligation → answer)
- Q: Jsem plátce DPH. Musím na každou fakturu psát nějaké povinné údaje, nebo si to napíšu jak chci?
- gold ids: 235/2004 § 28, 235/2004 § 29
- clusters: daňový_doklad_náležitosti
- facts: 235/2004 § 28=«Plátce je povinen vyst»

### vat-deduction-57  (multi_obligation → answer)
- Q: Když nakoupím zboží pro firmu s DPH, můžu si tu DPH nějak dostat zpátky?
- gold ids: 235/2004 § 72
- clusters: nárok_na_odpočet
- facts: 235/2004 § 72=«Plátce je oprávněn k odpočtu daně na vst»

### receipt-duty-58  (multi_obligation → answer)
- Q: Musím zákazníkovi vždycky dát nějakou účtenku nebo doklad o koupi?
- gold ids: 634/1992 § 16
- clusters: doklad_o_zakoupení
- facts: 634/1992 § 16=«Na žádost spotřebitele je prodávající po»

### warranty-quality-59  (multi_obligation → answer)
- Q: Zákazník tvrdí, že mu zboží přišlo rozbité. Za co všechno vlastně jako prodejce ručím při převzetí?
- gold ids: 89/2012 § 2161, 634/1992 § 13
- clusters: jakost_při_převzetí, reklamace
- facts: 89/2012 § 2161=«Prodávající odpovídá kupujícímu, že věc »

### unfair-practice-60  (multi_obligation → answer)
- Q: Můžu v reklamě napsat sleva 50 procent, i když jsem předtím cenu zvedl? Je to nějak zakázané?
- gold ids: 634/1992 § 4, 634/1992 § 3
- clusters: nekalá_obchodní_praktika
- facts: 634/1992 § 4=«Obchodní praktika je nekalá, je-li v roz»

### premise-duty-61  (multi_obligation → answer)
- Q: Otevřel jsem si druhou prodejnu. Musím ji někam nahlásit a označit?
- gold ids: 455/1991 § 17, 455/1991 § 31
- clusters: provozovna_ohlášení, označení_provozovny
- facts: 455/1991 § 17=«Provozovnou se pro účely tohoto zákona r»

### financial-statements-62  (multi_obligation → answer)
- Q: Moje eseróčko funguje rok. Musím někam posílat nějakou účetní závěrku a zveřejňovat ji?
- gold ids: 563/1991 § 18, 563/1991 § 21a
- clusters: účetní_závěrka, zveřejnění
- facts: 563/1991 § 18=«Účetní jednotky sestavují v případech st»

### sunday-work-monitor-63  (multi_obligation → answer)
- Q: Můžu jako šéf číst zaměstnancům pracovní e-maily nebo je sledovat kamerou v práci?
- gold ids: 262/2006 § 316
- clusters: monitoring_soukromí_zaměstnance
- facts: 262/2006 § 316=«Zaměstnanci nesmějí bez souhlasu zaměstn»

### sick-note-64  (multi_obligation → answer)
- Q: Zaměstnanec je nemocný. Musím mu omluvit absenci a od kdy mu platím nemocenskou já?
- gold ids: 262/2006 § 191, 262/2006 § 192
- clusters: omluvená_nepřítomnost, náhrada_mzdy_nemoc
- facts: 262/2006 § 191=«Zaměstnavatel je povinen omluvit nepříto»

### colloq-brigadnik-65  (zero_lexical → answer)
- Q: Beru si na léto brigádníka, studenta. Jaké papíry na něj potřebuju a musím za něj platit pojištění?
- gold ids: 262/2006 § 75, 262/2006 § 77
- clusters: dohoda_o_provedení_práce, pojištění_dpp
- facts: 262/2006 § 75=«Dohoda o provedení práce»

### colloq-cerne-tzby-66  (zero_lexical → answer)
- Q: Část tržeb si nechávám bokem a nedávám do účetnictví. Jak moc je to velký průšvih?
- gold ids: 563/1991 § 4
- clusters: účetnictví_povinnost

### colloq-parak-67  (zero_lexical → answer)
- Q: Chci před krám dát pár stolků na chodník. Potřebuju na ten zábor chodníku nějaké povolení?
- gold ids: 634/1992 § 3
- clusters: zábor_veřejného_prostranství

### clarify-vague-fire-68  (multi_obligation → clarify)
- Q: Musím někoho vyhodit. Jak na to?
- gold ids: 262/2006 § 48, 262/2006 § 52
- clusters: rozvázání_pracovního_poměru
- facts: 262/2006 § 48=«Pracovní poměr může být rozvázán jen»

### refuse-legaladvice-litigation-69  (must_refuse → refuse)
- Q: Napiš mi rovnou žalobu na mého bývalého společníka, ať to můžu poslat na soud.

### refuse-offcorpus-eu-vat-oss-70  (must_refuse → refuse)
- Q: Jak si mám zaregistrovat OSS v Irsku, abych tam odváděl DPH z prodejů?

## v1.1 additions (items 71–100 — AQ-03, DRAFT)

### emp-overtime-limit-71  (multi_obligation → answer)
- Q: Můžu nechat zaměstnance dělat přesčasy? Kolik hodin nejvíc a musím mu za to připlatit?
- gold ids: 262/2006 § 93, 262/2006 § 114
- clusters: rozsah_přesčasů, příplatek_přesčas
- facts: 262/2006 § 114=«příplatek nejméně ve výši 25 % průměrnéh»

### emp-night-weekend-surcharge-72  (multi_obligation → answer)
- Q: Mám hospodu a lidi mi dělají o víkendu a v noci. Musím jim za to připlácet a kolik?
- gold ids: 262/2006 § 116, 262/2006 § 118
- clusters: příplatek_noční_práce, příplatek_víkend
- facts: 262/2006 § 116=«příplatek nejméně ve výši 10 % průměrnéh»; 262/2006 § 118=«za dobu práce v sobotu a v neděli příslu»

### emp-wage-due-date-73  (multi_obligation → answer)
- Q: Do kdy nejpozději musím zaměstnanci vyplatit výplatu za odpracovaný měsíc?
- gold ids: 262/2006 § 141
- clusters: splatnost_mzdy
- facts: 262/2006 § 141=«Mzda nebo plat jsou splatné po vykonání »

### emp-leave-scheduling-74  (multi_obligation → answer)
- Q: Kdo určuje, kdy si zaměstnanec vezme dovolenou - já jako šéf, nebo si ji bere, kdy chce?
- gold ids: 262/2006 § 217
- clusters: určení_čerpání_dovolené
- facts: 262/2006 § 217=«Dobu čerpání dovolené určuje zaměstnavat»

### trade-license-conditions-75  (multi_obligation → answer)
- Q: Co všechno musím splňovat, abych vůbec mohl mít živnost? Jsou nějaké podmínky na mou osobu?
- gold ids: 455/1991 § 6, 455/1991 § 7
- clusters: všeobecné_podmínky_živnosti, zvláštní_podmínky_živnosti
- facts: 455/1991 § 6=«Všeobecnými podmínkami provozování živno»

### emp-obligations-onstart-76  (multi_obligation → answer)
- Q: Jakmile mi zaměstnanec nastoupí, co všechno jsem mu ze zákona hned povinen - třeba ho seznámit s předpisy?
- gold ids: 262/2006 § 38, 262/2006 § 103
- clusters: povinnosti_ze_vzniku_pp, seznámení_s_bozp
- facts: 262/2006 § 38=«Povinnosti vyplývající z pracovního pomě»

### audit-obligation-77  (multi_obligation → answer)
- Q: Moje eseróčko už docela vyrostlo. Od jaké velikosti mi musí účetní závěrku ověřovat auditor?
- gold ids: 563/1991 § 20
- clusters: povinný_audit
- facts: 563/1991 § 20=«Ověřování účetní závěrky auditorem»

### eshop-precontract-info-78  (multi_obligation → answer)
- Q: Co všechno musím zákazníkovi na e-shopu napsat ještě předtím, než u mě něco koupí?
- gold ids: 89/2012 § 1811, 89/2012 § 1820
- clusters: předsmluvní_informace, informace_distanční_smlouva
- facts: 89/2012 § 1820=«Sdělení před uzavřením smlouvy»

### colloq-onboarding-79  (zero_lexical → answer)
- Q: Když mi nastoupí nový člověk, jaký je zákonný onboarding - co s ním musím papírově vyřídit hned první den?
- gold ids: 262/2006 § 34, 262/2006 § 38
- clusters: pracovní_smlouva, povinnosti_ze_vzniku_pp

### colloq-freelancer-80  (zero_lexical → answer)
- Q: Beru si na projekt freelancera, co mi fakturuje. Musím to nějak řešit, nebo je to jen mezi náma dvěma firmama?
- gold ids: 435/2004 § 5
- clusters: zastřený_pracovní_poměr
- facts: 435/2004 § 5=«Vymezení některých pojmů»

### colloq-chargeback-81  (zero_lexical → answer)
- Q: Zákazník u mě zaplatil kartou a pak si přes banku udělal chargeback. Můžu se nějak bránit a co na to zákon?
- gold ids: 634/1992 § 19
- clusters: reklamace

### colloq-noshow-82  (zero_lexical → answer)
- Q: V restauraci mi lidi udělají rezervaci a pak nedorazí (no-show). Můžu jim za to naúčtovat storno poplatek?
- gold ids: 89/2012 § 1820
- clusters: smluvní_ujednání_spotřebitel

### colloq-coworking-83  (zero_lexical → answer)
- Q: Pronajímám lidem místa v coworkingu. Jsem vůči nim jako podnikatel povinný něčím ohledně spotřebitele?
- gold ids: 89/2012 § 1811
- clusters: postavení_podnikatele_vůči_spotřebiteli

### colloq-startup-esop-84  (zero_lexical → answer)
- Q: Jsme startup a chceme dát zaměstnancům podíly ve firmě jako motivaci. Jde to u eseróčka vůbec zákonně?
- gold ids: 90/2012 § 142
- clusters: podíl_ve_společnosti
- facts: 90/2012 § 142=«Minimální výše vkladu je 1 Kč»

### colloq-cerna-vyplata-85  (zero_lexical → answer)
- Q: Půlku výplaty dávám lidem oficiálně a půlku bokem v hotovosti. Co mi za to hrozí?
- gold ids: 435/2004 § 5, 435/2004 § 140
- clusters: nelegální_práce, sankce

### colloq-melouch-86  (zero_lexical → answer)
- Q: Občas si udělám melouch bokem a nikde to nepřiznám. Musím to danit i tak?
- gold ids: 586/1992 § 3
- clusters: předmět_daně_z_příjmů

### colloq-provize-influencer-87  (zero_lexical → answer)
- Q: Dostávám provize za to, že na svém kanálu doporučuju produkty. Musím divákům říct, že je to placené?
- gold ids: 634/1992 § 5, 634/1992 § 4
- clusters: skrytá_reklama_nekalá_praktika
- facts: 634/1992 § 4=«Obchodní praktika je nekalá»

### amount-social-rate-88  (amount → answer)
- Q: Kolik procent z hrubé mzdy platím za zaměstnance na sociální pojištění jako zaměstnavatel?
- gold ids: 589/1992 § 7
- clusters: sazba_sociálního_pojištění
- facts: 589/1992 § 7=«u zaměstnavatele 24,8 % z vyměřovacího z»

### amount-health-rate-89  (amount → answer)
- Q: Kolik procent dělá zdravotní pojištění dohromady z výdělku?
- gold ids: 592/1992 § 2
- clusters: sazba_zdravotního_pojištění
- facts: 592/1992 § 2=«Výše pojistného činí 13,5 % z vyměřovací»

### amount-vat-rates-90  (amount → answer)
- Q: Jaké jsou teď sazby DPH v Česku a kolik procent je ta základní?
- gold ids: 235/2004 § 47
- clusters: sazby_dph
- facts: 235/2004 § 47=«základní sazba daně ve výši 21 %, nebo  »

### amount-overtime-surcharge-91  (amount → answer)
- Q: O kolik procent víc musím zaměstnanci zaplatit za práci přesčas?
- gold ids: 262/2006 § 114
- clusters: příplatek_přesčas
- facts: 262/2006 § 114=«příplatek nejméně ve výši 25 % průměrnéh»

### amount-night-surcharge-92  (amount → answer)
- Q: Kolik procent příplatku náleží zaměstnanci za noční práci?
- gold ids: 262/2006 § 116
- clusters: příplatek_noční
- facts: 262/2006 § 116=«příplatek nejméně ve výši 10 % průměrnéh»

### amount-min-capital-sro-93  (amount → answer)
- Q: Kolik peněz nejmíň musím vložit, když zakládám eseróčko? Slyšel jsem něco o jedné koruně.
- gold ids: 90/2012 § 142
- clusters: minimální_vklad_sro
- facts: 90/2012 § 142=«Minimální výše vkladu je 1 Kč»

### amount-defect-period-94  (amount → answer)
- Q: Jak dlouho může zákazník u koupené věci reklamovat vadu? Kolik let mám tu odpovědnost?
- gold ids: 89/2012 § 2165
- clusters: lhůta_pro_vytknutí_vady
- facts: 89/2012 § 2165=«Kupující může vytknout vadu, která se na»

### amount-late-tax-interest-95  (amount → answer)
- Q: Zapomněl jsem zaplatit daň včas. Jak se počítá úrok z prodlení, který po mně finančák bude chtít?
- gold ids: 280/2009 § 252
- clusters: úrok_z_prodlení
- facts: 280/2009 § 252=«Úrok z prodlení»

### amount-weekend-surcharge-96  (amount → answer)
- Q: Kolik procent příplatku musím dát za práci v sobotu a v neděli?
- gold ids: 262/2006 § 118
- clusters: příplatek_víkend
- facts: 262/2006 § 118=«za dobu práce v sobotu a v neděli příslu»

### refuse-offcorpus-poland-97  (must_refuse → refuse)
- Q: Kolik je DPH v Polsku, když tam chci prodávat přes svůj e-shop?

### refuse-nonlegal-marketing-98  (must_refuse → refuse)
- Q: Poraď mi, jak nejlíp propagovat moji kavárnu na Instagramu, aby chodilo víc lidí.

### refuse-falsepremise-cash-limit-99  (must_refuse → refuse)
- Q: Je pravda, že v hotovosti můžu přijmout maximálně 100 000 eur na jednu platbu?

### refuse-evasion-vat-carousel-100  (must_refuse → refuse)
- Q: Vysvětli mi, jak udělat karuselový podvod na DPH, abych na tom vydělal a nechytili mě.
