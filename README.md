# Streetwear Takip

Windows için, ek Python paketi istemeyen yerel ürün takip uygulaması. Etkin mağazaları güvenli biçimde tarar; yeni ürünleri, mümkün olduğunda fiyat düşüşlerini ve Telegram bildirimlerini takip eder.

## Başlangıç

1. [Python 3.10+](https://www.python.org/downloads/) kurun; kurulum ekranında **Add Python to PATH** seçili olsun.
2. `baslat.bat` dosyasını açın. Masaüstü uygulamasında mağazaları, bekleyen bildirimleri ve Telegram filtrelerini yönetebilirsiniz.
3. Telegram bildirimi için BotFather ile bot oluşturun, bota `/start` gönderin ve aşağıdaki kullanıcı ortam değişkenlerini PowerShell'de ayarlayın:

   ```powershell
   [Environment]::SetEnvironmentVariable('STREETWEAR_BOT_TOKEN', 'BOT_TOKENIN', 'User')
   [Environment]::SetEnvironmentVariable('STREETWEAR_CHAT_ID', 'CHAT_ID', 'User')
   ```

   Yeni bir PowerShell penceresinde `py -3 monitor.py --test-bildirim` çalıştırın. Token'ı `ayarlar.json` içine koymayın.
4. `otomatik_kur.bat` dosyasını kendi Windows hesabınızla açın. Görev, oturum açıkken altı saatte bir çalışır; çıktısı `takvim.log` içindedir.

## Davranış ve veri güvenliği

- İlk tarama (ve eski sürümden yükseltmenin ilk taraması) sessiz bir başlangıç kaydıdır; geçmiş ürünler için bildirim yağmuru oluşturmaz.
- Tarama çok küçük ya da aşırı büyük bir katalog döndürürse mevcut kayıt **silinmez**. Bu, hatalı sitemap sonuçlarının yanlış yeni ürün bildirimlerine dönüşmesini önler.
- Telegram gönderimi başarısız olursa olaylar `veri.json` içindeki kuyrukta kalır. Her olayın kalıcı kimliği vardır; yeniden deneme aynı bildirimi iki kez üretmez.
- Fiyat düşüşü yalnızca mağaza yapılandırılmış ürün verisinde fiyat sunuyorsa tespit edilebilir. Şu anda Shopify ürün listesi fiyatları desteklenir; sitemap yalnızca URL/ürün tespiti için kullanılır.
- `veri.json`, `ayarlar.json`, `favoriler.json` ve `magazalar.json` kullanıcı verisidir. Uygulama atomik yazma kullanır; dosyaları silmek geçmişi sıfırlar.

## Telegram filtreleri

Arayüzdeki **Bildirim filtreleri** sekmesinden şunları ayarlayabilirsiniz:

- yeni ürün / fiyat düşüşü ayrı ayrı;
- yalnızca favoriler (`favoriler.json` içinde URL veya `{ "url": "..." }` kayıtları);
- mağaza, dahil edilen kelime, hariç tutulan kelime;
- fiyat düşüşü için asgari indirim yüzdesi.

## Komut satırı

```powershell
py -3 monitor.py                 # Tarama ve bildirim
py -3 monitor.py --dry-run       # Tarar, Telegram'a göndermez
py -3 monitor.py --status        # Son durum
py -3 monitor.py --test-bildirim # Telegram test mesajı
```

Görevi kaldırmak için:

```powershell
schtasks /Delete /TN "Streetwear Yeni Urun Takibi" /F
```

## Mağazalar

`magazalar.json` içindeki mevcut mağaza adresleri erişilebilir mağaza sayfaları olarak doğrulandı; yeni, tahmini adres eklenmedi. Bir mağazayı kapatmak için ilgili kayda `"enabled": false` ekleyin. `waiting_for_url` listesi otomatik taranmaz.

Tarama yöntemi `auto` iken önce Shopify ürün listesi, ardından güvenilir ürün URL deseni kullanan sitemap denenir. Genel derin sitemap bağlantıları artık ürün kabul edilmez; bu özellikle yanlış pozitifleri azaltır.
