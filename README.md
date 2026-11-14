# Jev & Ollaya — Yerel Karar Motoru ve CLI Üst Router (`jroute`)

Bu klasör (`/home/efe/Desktop/projeler/jev`), **TypeSafe AI Jev SDK / CLI** araçlarını, yerel Jev uyumlu karar modeli sunucusu **Ollaya** yapılandırmasını ve terminalden verilen görevleri tek adımda analiz ederek en uygun aracı (**`claude`** veya **`agy`**) ve alt modeli seçip başlatan **`jroute` (`jev-router`)** betiğini içerir.

## 1. Kurulu Bileşenler

| Bileşen | Konum / Paket | Açıklama |
| :--- | :--- | :--- |
| **Ollaya Sunucusu** | `/home/efe/.local/bin/ollaya` (`ollaya.service`) | `http://127.0.0.1:11435` adresinde `/v1/systemone` Jev API uyumlu yerel karar modeli sunucusu |
| **Jev CLI** | `/home/efe/.local/bin/jev` | Terminalden `jev noul`, `jev choice`, `jev score`, `jev eval` çalıştıran CLI (yerel Ollaya'ya bağlı) |
| **TypeSafe Python SDK** | `/home/efe/Desktop/projeler/jev/.venv` (`typesafe-sdk`) | Resmi Python SDK (`TypeSafeClient`, `Choice`, `Score`, `Noul`) |
| **TypeSafe JS/TS SDK** | `/home/efe/Desktop/projeler/jev/node_modules/@typesafe-ai/sdk` | Resmi TypeScript/JavaScript SDK |
| **Üst Router Betiği** | `/home/efe/Desktop/projeler/jev/router.py` (`jroute` / `jev-router`) | Görevi tek adımda analiz edip `claude` veya `agy` + alt model + efor seçerek başlatan betik |
| **Etkileşimli Konsol** | `/home/efe/Desktop/projeler/jev/jrc.py` (`jrc`) | Terminalde `jrc` yazıldığında mesaj girilebilen etkileşimli CLI konsol arayüzü |

## 2. Üst Router (`jroute` & `jrc`) Kullanımı

Terminalde doğrudan konsol arayüzünü açmak için `jrc`, tek satırda görev vermek için `jroute` komutunu kullanabilirsiniz:

```bash
# 1. Etkileşimli Konsol Arayüzü (jrc):
jrc
# Konsol açıldığında mesajınızı doğrudan yazıp Enter'a basabilirsiniz.
# Konsol içi komutlar:
#   /claude, /agy, /auto  -> Hedef aracı sabitle veya otomatiğe al
#   /d, !d <mesaj>        -> Komutu başlatmadan sadece Jev kararını test et
#   /p, !p <mesaj>        -> Tek seferlik (--print) modda çalıştırıp konsola dön
#   /y                    -> --dangerously-skip-permissions modunu aç/kapat
#   /m                    -> Çok satırlı mesaj girişi
#   /cd <dizin>           -> Çalışma dizinini değiştir
#   /models, /help, q     -> Profil listesi, yardım ve çıkış

# 2. Doğrudan Tek Komutla Çalıştırma (jroute):
jroute "eretnastore.com sepet sayfasındaki mobil görünüm hatasını düzelt"

# Tek seferlik (--print / -p) modda çalıştırmak için:
jroute -p "log dosyasında son 20 satırdaki hataları özetle"

# Aracı zorlayıp yalnızca alt modeli ve efor seviyesini Jev'e seçtirmek için:
jroute -t claude "odoo stok hareketleri sorgusunu optimize et"
jroute -t agy "xaura.net için yeni sonuçlar sayfası tasarla"

# Komutu çalıştırmadan sadece Jev kararını ve üretilen komutu görmek için (--dry-run / -d):
jroute -d "commit push"

# Karar detaylarını ve olasılık dağılımını JSON olarak görmek için:
jroute --json "PostgreSQL deadlock sorununu analiz et"
```

### Desteklenen Alt Model Profilleri

Router, tek bir `client.system_one(...)` çağrısında görevin içeriğini, çalışma dizini bağlamını (`cwd`, dosya yapısı, teknoloji yığını), karmaşıklık seviyesini (`Score`) ve kritiklik olasılığını (`Noul`) değerlendirerek şu profiller arasından seçim yapar:

- **`claude` (Claude Code CLI)**
  - `claude-haiku-5-5` (`effort=low`): Hızlı git işlemleri (`commit`, `push`), küçük betik ve metin düzenlemeleri
  - `claude-sonnet-5-5` (`effort=medium|high`): Standart yazılım geliştirme, hata ayıklama (bugfix), refactoring ve test yazımı
  - `claude-opus-5-5` (`effort=high|xhigh`): Karmaşık çok dosyalı mimari değişiklikler, kritik veritabanı/backend (Odoo, PostgreSQL) sorunları
- **`agy` (Antigravity CLI)**
  - `gemini-3.8-flash-low|medium` (`effort=low`): Hızlı durum kontrolü, log inceleme, tek adımlık sorgular
  - `gemini-4-argon-medium|high` (`effort=medium|high`): Tam yığın (full-stack) web geliştirme, UI/UX tasarım, API/bot entegrasyonu, çok adımlı otomasyon
  - `gemini-4-hydrogen-high|xhigh` (`effort=high|xhigh`): Derin araştırma, karmaşık sistem tasarımı ve çoklu ajan orkestrasyonu

## 3. Doğrudan `jev` CLI ve `ollaya` Kullanımı

```bash
# Evet/Hayır olasılığı (Noul):
jev noul "Bu mesaj acil mi?" --state "Sunucu çöktü, siparişler düşmüyor!"

# Çoktan seçmeli karar (Choice):
jev choice "Hangi departman?" --state "Faturam iki kez kesildi" \
  --option muhasebe="Ödeme ve fatura" --option teknik="Yazılım hatası" --option diger

# Yerel modelleri listeleme:
ollaya list
```
