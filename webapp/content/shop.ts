// GENERATO da pod-shop/catalog.py — non modificare a mano.
// Rigenera con: venv/bin/python catalog.py --write-webapp

export type ShopProduct = {
  id: string
  name: string
  price: number
  image: string
  imageW: number
  imageH: number
  colors: string[]
  sizes: string[]
  url?: string
}

export const SHOP_CURRENCY = "EUR"

export const SHOP_PRODUCTS: ShopProduct[] = [
  {
    id: "tech-tee-ctrl-alt-regala-light",
    name: "CTRL + ALT + REGALA",
    price: 24.99,
    image: "/shop/card/tech-tee-ctrl-alt-regala-light.webp",
    imageW: 900,
    imageH: 1200,
    colors: ["white"],
    sizes: ["S", "M", "L", "XL", "2XL"],
  },
  {
    id: "tech-tee-404-regalo-light",
    name: "404 — REGALO NON TROVATO",
    price: 24.99,
    image: "/shop/card/tech-tee-404-regalo-light.webp",
    imageW: 900,
    imageH: 1200,
    colors: ["white"],
    sizes: ["S", "M", "L", "XL", "2XL"],
  },
  {
    id: "tech-tee-carica-al-100-light",
    name: "CARICA AL 100%",
    price: 24.99,
    image: "/shop/card/tech-tee-carica-al-100-light.webp",
    imageW: 900,
    imageH: 1200,
    colors: ["white"],
    sizes: ["S", "M", "L", "XL", "2XL"],
  },
  {
    id: "tech-tee-modalita-aereo-light",
    name: "MODALITA' AEREO",
    price: 24.99,
    image: "/shop/card/tech-tee-modalita-aereo-light.webp",
    imageW: 900,
    imageH: 1200,
    colors: ["white"],
    sizes: ["S", "M", "L", "XL", "2XL"],
  },
  {
    id: "tech-tee-sistema-aggiornato-light",
    name: "SISTEMA AGGIORNATO",
    price: 24.99,
    image: "/shop/card/tech-tee-sistema-aggiornato-light.webp",
    imageW: 900,
    imageH: 1200,
    colors: ["white"],
    sizes: ["S", "M", "L", "XL", "2XL"],
  },
]
