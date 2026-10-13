# Como as vagas são calculadas — os algoritmos passo a passo

Este documento explica, com a matemática por trás, como o pipeline transforma
linhas de vias e faces de quadra em pontos de vaga de plantio. Os exemplos
numéricos foram gerados rodando as próprias funções do
[`scripts/03_allocate_slots.py`](scripts/03_allocate_slots.py).

Sumário:

1. [Notação: a via como uma curva](#1-notação-a-via-como-uma-curva)
2. [Preparação: vias contínuas e esquinas](#2-preparação-vias-contínuas-e-esquinas)
3. [Largura do leito carroçável](#3-largura-do-leito-carroçável)
4. [Deslocar um ponto para a calçada](#4-deslocar-um-ponto-para-a-calçada)
5. [Trechos bloqueados: esquinas e obstáculos](#5-trechos-bloqueados-esquinas-e-obstáculos)
6. [A caminhada: onde cai cada vaga](#6-a-caminhada-onde-cai-cada-vaga)
7. [Os cinco métodos](#7-os-cinco-métodos)
8. [Fachadas e co-registro com as edificações](#8-fachadas-e-co-registro-com-as-edificações)
9. [Pós-processamento comum a todos os métodos](#9-pós-processamento-comum-a-todos-os-métodos)
10. [Canteiros centrais](#10-canteiros-centrais)
11. [Resumo dos parâmetros](#11-resumo-dos-parâmetros)

---

## 1. Notação: a via como uma curva

Todas as contas são feitas em **metros**, numa projeção plana (SIRGAS 2000 /
UTM do município — o `epsg` do registro em `municipalities.py`). Nada é
calculado em graus de latitude/longitude.

Uma via do OSM (ou uma face de quadra do IBGE) é uma **polilinha**: uma
sequência de vértices ligados por segmentos retos. Nós a descrevemos por
**comprimento de arco** $s$, a distância percorrida desde o início da linha:

```math
\gamma(s) = (x(s),\ y(s)), \qquad 0 \le s \le L
```

onde $L$ é o comprimento total. Em código, $\gamma(s)$ é
`line.interpolate(s)` e o caminho inverso — dado um ponto $P$, achar o $s$ do
ponto da linha mais próximo dele — é `line.project(P)`.

**Tangente e normal.** A direção da via num ponto é estimada por uma
diferença finita de ±0,5 m em torno de $s$:

```math
\mathbf{t}(s) = \frac{\gamma(s+0{,}5) - \gamma(s-0{,}5)}{\lVert \gamma(s+0{,}5) - \gamma(s-0{,}5) \rVert}
```

e a **normal à esquerda** (no sentido em que a via foi desenhada) é a
tangente girada 90° no sentido anti-horário:

```math
\mathbf{t} = (t_x,\ t_y) \quad\Longrightarrow\quad \mathbf{n} = (-t_y,\ t_x)
```

Ver `point_at_offset` e `probe_frames`. O lado "esquerdo" de uma via depende
de como o mapeador a desenhou no OSM — não tem relação com o lado par ou
ímpar da numeração.

**Rumo local.** Quando o código precisa comparar a direção de duas vias,
usa o ângulo da tangente numa janela de ±2 m (`local_bearing`):

```math
\theta(s) = \operatorname{atan2}\big(y(s+2) - y(s-2),\ x(s+2) - x(s-2)\big)
```

Duas direções são comparadas sem levar em conta o sentido de desenho:
$\Delta\theta = \min(|\theta_1 - \theta_2|,\ |\theta_1 - \theta_2 \pm 180°|)$
(`angle_diff`).

---

## 2. Preparação: vias contínuas e esquinas

O OSM quebra uma mesma rua em vários trechos (*ways*) toda vez que alguma tag
muda: ponte, pavimento, limite de velocidade. Se cada trecho fosse tratado
como uma rua separada, cada emenda ganharia um afastamento de esquina dos dois
lados e reiniciaria a contagem do espaçamento — um buraco de 12 m ou mais
onde não existe cruzamento.

`build_road_chains` resolve isso analisando **cada ponta** de cada trecho, e
contando quantas outras vias tocam aquele ponto (tolerância de 5 cm):

| O que toca a ponta | Classificação | É esquina? |
|---|---|---|
| nenhuma outra via | rua sem saída | não |
| exatamente uma, que também termina ali | **emenda** do OSM | não |
| exatamente uma, que passa pelo meio | cruzamento em **T** | sim |
| duas ou mais | cruzamento | sim |

Trechos ligados por uma emenda e que têm o mesmo nome, a mesma classe e a
mesma largura são unidos numa só linha contínua. A união usa uma estrutura
*union-find* (cada trecho aponta para um representante do seu grupo) e
depois `shapely.line_merge` costura as geometrias. Cada via contínua resultante
carrega dois indicadores, `start_corner` e `end_corner`, que dizem se suas
pontas são esquinas de verdade.

Só entram vias de classes que recebem vagas — `residential`, `living_street`,
`unclassified`, `tertiary`, `secondary`, `primary` e seus `_link`
(`GENERATION_CLASSES`). Vias de serviço e trilhas não contam como esquina
(`CORNER_CLASSES`).

---

## 3. Largura do leito carroçável

A vaga precisa ficar fora do leito carroçável. Como nenhuma fonte aberta mede
o meio-fio, a meia-largura $h$ do leito é **estimada** a partir das tags do
OSM (`carriageway_half_width` em
[`planting_slots/osm.py`](planting_slots/osm.py)):

1. Parte-se da **largura padrão da classe** $W_c$ — por exemplo, 9 m para
   `residential`, 12 m para `secondary`, 14 m para `primary`
   (`DEFAULT_WIDTH_BY_CLASS`). Os valores são propositalmente generosos: na
   dúvida, o erro deve empurrar a vaga para a calçada, não para a rua.
2. Se a via é uma das pistas de uma via dupla (duas vias de mão única com o
   mesmo nome, paralelas e em sentidos opostos — `find_divided_pairs`), cada
   pista recebe $0{,}6\,W_c$ ($f_{\text{dupla}}$ abaixo).
3. Uma tag `width=` plausível (entre 3 m e $2{,}5\,W_c$), ou `lanes=` (com
   $3{,}5\ \text{m} \times \text{faixas} + 2\ \text{m}$), dá uma meia-largura
   $h_{\text{tag}}$. Ela só é considerada se for **maior** que o padrão — uma
   tag nunca estreita a via — e, como no OSM é comum `width=` medir a rua
   inteira (com calçadas), é lida como largura total: desconta-se a largura
   mínima de calçada da região $c_{\min}$ (2,2 m no Sudeste, 3,0 m no Sul,
   1,5 m no Centro-Oeste — `MIN_SIDEWALK_WIDTH_BY_REGION_M`).

```math
h_0 = \frac{W_c \cdot f_{\text{dupla}}}{2}, \qquad
h = \begin{cases}
h_0 & \text{sem tag, ou } h_{\text{tag}} \le h_0 \\
\max(h_0,\ h_{\text{tag}} - c_{\min}) & \text{caso contrário}
\end{cases}
```

onde $f_{\text{dupla}} = 0{,}6$ para pistas de via dupla e $1$ nas demais.

O ponto da vaga, nos métodos de calçada, fica a uma distância $d$ do eixo
igual à meia-largura mais um recuo de 0,5 m a partir do meio-fio
(`CURB_SETBACK_M`):

```math
d = h + 0{,}5\ \text{m}
```

Exemplo: numa rua `residential` sem tags de largura, $h = 4{,}5$ m e a vaga
fica a $d = 5{,}0$ m do eixo.

---

## 4. Deslocar um ponto para a calçada

Com a tangente e a normal da seção 1, o ponto deslocado a uma distância $d$
do eixo, na posição $s$ e no lado $\sigma$ ($+1$ = esquerdo, $-1$ =
direito), é:

```math
P_\sigma(s) = \gamma(s) + \sigma \, d \, \mathbf{n}(s)
```

Isso é `point_at_offset(road, s, side_left, d)`. Com $d = 0$, o ponto fica
sobre o eixo — o método `osm_eixo`.

```
              P₊(s)  ●   ← calçada esquerda (σ = +1)
                     │ d
  ───────────────────●──────────────────▶  eixo γ(s), sentido de desenho
                     │ d
              P₋(s)  ●   ← calçada direita (σ = −1)
```

---

## 5. Trechos bloqueados: esquinas e obstáculos

Antes de caminhar, cada via (ou face) recebe uma lista de **intervalos
proibidos** em $s$, cada um com o seu motivo (`blocked_intervals`):

- **Esquina** — 6 m livres em cada ponta que é esquina de verdade
  (`CORNER_SETBACK_M`, Coletânea MMA 2026):

  ```math
  [0,\ 6] \quad\text{e}\quad [L-6,\ L]
  ```

- **Obstáculo do OSM** — cada obstáculo a até 8 m da linha é projetado sobre
  ela — sua posição $s_o$ é `line.project(obstáculo)` — e bloqueia um raio $r$ em torno
  da projeção:

  ```math
  [s_o - r,\ s_o + r]
  ```

  | Obstáculo | Tags OSM | Raio $r$ |
  |---|---|---|
  | `travessia` | `highway=crossing`, `footway=crossing` | 3 m |
  | `acesso_garagem` | `amenity=parking_entrance`, `service=driveway` | 2 m |
  | `ponto_onibus` | `highway=bus_stop`, `public_transport=platform` | 12 m |
  | `arvore_existente` | `natural=tree` | 4 m |

Intervalos podem se sobrepor; nesse caso eles formam um único trecho
bloqueado contínuo.

---

## 6. A caminhada: onde cai cada vaga

O coração do algoritmo é `allocate_face`, uma caminhada gulosa ao longo de
$s$. Em pseudocódigo:

```
s ← 0
enquanto s ≤ L:
    se s não está em nenhum intervalo bloqueado:
        coloca uma vaga em s
        s ← avança(s, espaçamento)                    # ver 6.1
    senão:
        registra "vaga inviável em s, motivo = <intervalo>"
        fim ← fim do trecho bloqueado (encadeando sobreposições)
        s ← min( avança(s, espaçamento),  fim + 0,1 m )   # ver 6.2
```

### 6.1 O passo: medir em linha reta, não ao longo da curva

O espaçamento exigido entre duas vagas — 10 m, por exemplo — é uma distância
**em linha reta** entre as duas árvores. Mas caminhar 10 m ao longo de uma
curva leva a um ponto que está a **menos** de 10 m em linha reta, porque a
corda de um arco é sempre menor que o arco:

```math
\text{corda} = 2R\,\sin\!\left(\frac{\Delta s}{2R}\right) \;<\; \Delta s
```

Por isso a função `advance` não avança simplesmente $\Delta s = 10$ m: ela
procura o **primeiro** $s' \ge s + e$ (em passos de 0,1 m) tal que a
distância em linha reta até a vaga anterior seja pelo menos o espaçamento
$e$:

```math
s' = \min\big\{\, s + e + 0{,}1k \;:\; k \in \mathbb{N},\ \ \lVert P_\sigma(s + e + 0{,}1k) - P_\sigma(s) \rVert \ge e \ \ \forall \sigma \,\big\}
```

Note o $\forall \sigma$: nos métodos de calçada a condição é checada **nos
pontos já deslocados**, nos dois lados que recebem vagas. Numa curva, o lado
de dentro é mais curto que o eixo e é ele que manda.

**Exemplo — reta.** Numa rua reta, corda e arco coincidem e
$s' = s + e$ exatamente: vagas a 0, 10, 20, 30 m…

**Exemplo — curva de raio $R = 20$ m, espaçamento 10 m.**

- No eixo ($d = 0$): o arco que dá uma corda de 10 m é
  $2R \arcsin(5/R) = 10{,}11$ m; arredondado para cima na grade de 0,1 m,
  `advance` devolve **10,2 m**.
- Na calçada ($d = 5$ m), dos dois lados: o lado de dentro tem raio
  $R - d = 15$ m. Andar $\Delta s$ no eixo corresponde a um ângulo
  $\Delta s / R$, e a corda no lado de dentro é
  $2(R-d)\sin\!\big(\Delta s / 2R\big)$. Para ela valer 10 m:

  ```math
  \Delta s = 2R\,\arcsin\!\left(\frac{5}{R-d}\right) = 40 \cdot \arcsin(1/3) = 13{,}59\ \text{m}
  ```

  e `advance` devolve **13,6 m**. Do lado de fora (raio 25 m) as vagas ficam
  um pouco mais afastadas que 10 m — é o preço de garantir os 10 m do lado
  de dentro.

Por que isso importa: a etapa final (seção 9.2) remove qualquer par de vagas
a menos do espaçamento, medido em linha reta. Se a caminhada medisse ao longo
do arco, a vaga seguinte cairia a 9,99 m da anterior e seria removida —
deixando um buraco de 20 m. Era exatamente isso que acontecia antes desta
correção.

A comparação usa uma tolerância de $10^{-6}$ m (`SPACING_TOL_M`), porque em
ponto flutuante uma distância de 10 m pode sair como 9,9999999996 m.

### 6.2 Depois de um bloqueio: retomar logo após o obstáculo

Quando a posição candidata cai num trecho bloqueado, a caminhada não pula
mais um espaçamento inteiro: ela retoma no que vier **primeiro** entre o
próximo passo normal e o fim do bloqueio + 0,1 m. Assim um obstáculo custa
só as vagas que de fato cobre.

**Exemplo** (gerado com o código): rua reta de 100 m, esquinas nas duas
pontas e uma travessia ocupando $[33,\ 39]$, espaçamento de 10 m.

| Passo | $s$ (m) | O que acontece |
|---|---|---|
| 1 | 0,0 | dentro da esquina $[0, 6]$ → inviável (`esquina`); retoma em $\min(10{,}0;\ 6{,}1) = 6{,}1$ |
| 2 | 6,1 | vaga |
| 3 | 16,1 | vaga |
| 4 | 26,1 | vaga |
| 5 | 36,1 | dentro da travessia → inviável (`travessia`); retoma em $\min(46{,}1;\ 39{,}1) = 39{,}1$ |
| 6 | 39,1 → 89,1 | vagas a cada 10 m |
| 7 | 99,1 | dentro da esquina $[94, 100]$ → inviável (`esquina`) |

Resultado: vagas em 6,1 · 16,1 · 26,1 · 39,1 · 49,1 · … · 89,1. O buraco
causado pela travessia é de 13 m (26,1 → 39,1), e não de 20 m.

Se a ponta inicial **não** é esquina (uma emenda, por exemplo), não há
bloqueio em $[0, 6]$ e a primeira vaga cai em $s = 0$.

---

## 7. Os cinco métodos

Todos usam a caminhada da seção 6. O que muda é **sobre qual linha** se
caminha e **como** a posição $s$ vira um ponto no mapa.

### 7.1 `osm_eixo` — eixo da via (padrão do webmap)

Caminha sobre a via contínua do OSM (seção 2) e coloca o ponto sobre o
próprio eixo: $P(s) = \gamma(s)$. O ponto fica no leito carroçável de
propósito; o método mostra a distribuição ao longo da rua (espaçamento,
esquinas, obstáculos), sem pretender acertar a posição lateral.

### 7.2 `osm_calcada_lado` — calçada em um dos lados

Igual ao 7.3, mas com um só lado $\sigma$ por via. O lado é escolhido pelos
endereços do IBGE (`choose_side_by_addresses`):

1. Cada face de quadra do IBGE é associada à via mais próxima que esteja a
   até 15 m do seu ponto médio $M$ e seja aproximadamente paralela a ela
   (diferença de rumo ≤ 30°).
2. O **lado** da face é o sinal do produto vetorial entre a direção da via
   e o vetor que vai da via até $M$. Com $R = \gamma(s_M)$ o ponto da via mais
   próximo de $M$ e $\theta$ o rumo local:

   ```math
   z = \cos\theta\,(M_y - R_y) - \sin\theta\,(M_x - R_x)
   \qquad
   \begin{cases} z > 0 & \text{face à esquerda} \\ z \le 0 & \text{face à direita} \end{cases}
   ```

   É o mesmo que perguntar se $M$ está do lado para onde aponta a normal
   $\mathbf{n}$.
3. Soma-se o total de endereços (`TOT_GERAL`) das faces de cada lado. Fica
   o lado com **menos** endereços (menos portões e acessos de garagem); se só
   um lado tem face do IBGE (uma rua ao longo de uma praça ou de um rio),
   esse lado; sem face nenhuma, o lado esquerdo.

### 7.3 `osm_calcada` — calçada nos dois lados

Caminha sobre a via contínua do OSM **sem** co-registro e coloca duas vagas
para cada posição $s$, uma de cada lado:

```math
P_{+1}(s) = \gamma(s) + d\,\mathbf{n}(s), \qquad P_{-1}(s) = \gamma(s) - d\,\mathbf{n}(s)
```

com $d = h + 0{,}5$ m (seção 3). Não consulta prédios: se a calçada real for
mais estreita que o estimado, a vaga pode cair dentro do lote.

### 7.4 `face_ibge` — face de quadra do IBGE

Caminha sobre a **linha da face de quadra** do IBGE, não sobre a via. Só
entram faces em setores censitários urbanos, de tipos que não sejam viela,
beco, escadaria etc., e cujo ponto médio esteja a até 15 m de uma via OSM
elegível e paralela (diferença de rumo ≤ 30°, `match_faces_to_streets`).

A linha da face, em muitas quadras, foi digitalizada rente ao eixo da via,
e não na divisa do lote. Por isso cada ponto $Q$ obtido na caminhada é
**ajustado lateralmente** em relação à via OSM pareada (já co-registrada às
edificações, seção 8.2) — `clamp_to_road`:

1. Projeta $Q$ na via: $R = \gamma(s_Q)$ e mede
   $\delta = \lVert Q - R \rVert$ e a direção
   $\mathbf{u} = (Q - R)/\delta$.
2. Define o **piso** (não pode ficar mais perto do eixo do que isso) e o
   **teto** (não pode passar da fachada):

   ```math
   d_{\min} = h + 0{,}5, \qquad d_{\max} = f - 0{,}5
   ```

   onde $f$ é a distância até a fachada naquele lado (seção 8.1); sem
   prédio mapeado por perto, $d_{\max} = \infty$.
3. Se $d_{\min} > d_{\max}$, não há espaço seguro entre o leito e a fachada:
   o candidato vira vaga inviável com motivo `sem_espaco_seguro`.
4. Senão, a distância final é a original "presa" no intervalo permitido:

   ```math
   d^{*} = \min\big(\max(\delta,\ d_{\min}),\ d_{\max}\big), \qquad P = R + d^{*}\,\mathbf{u}
   ```

5. Perto de curvas e cruzamentos, $P$ pode ficar mais perto de **outro**
   trecho da mesma via do que o piso permite. Nesse caso o ponto é projetado
   de novo e o ajuste se repete, até 6 vezes; se ainda não convergir, o
   candidato é descartado.

Este é o único método que também gera vagas em canteiros centrais (seção 10).

### 7.5 `osm_overture` — calçada ajustada com prédios

Como o 7.3 (dois lados, $d = h + 0{,}5$), mas sobre a via OSM
**co-registrada** às edificações (seção 8.2) e com o teto da fachada:

```math
\text{se}\quad h + 0{,}5 \;>\; f_\sigma(s) - 0{,}5 \quad\Rightarrow\quad \text{inviável (sem\_espaco\_seguro)}
```

caso contrário $P_\sigma(s) = \gamma(s) + \sigma\,d\,\mathbf{n}(s)$, desde
que o ponto deslocado continue a pelo menos $d$ da via (de novo, curvas
fechadas podem violar isso).

---

## 8. Fachadas e co-registro com as edificações

### 8.1 Distância até a fachada

Para cada via, a cada 5 m ao longo de $s$, traçam-se dois raios
perpendiculares de 40 m, um para cada lado, e mede-se a distância até a
primeira edificação da Overture que eles cruzam (`facade_probes` em
[`planting_slots/buildings.py`](planting_slots/buildings.py)).

A distância da fachada numa posição $s$ é o **percentil 25** das medições
numa janela de ±15 m em torno de $s$, desde que haja pelo menos 2 medições
(`local_facade`):

```math
f_\sigma(s) = \operatorname{P}_{25}\big\{\, \text{raio}_\sigma(s_k) \;:\; |s_k - s| \le 15 \,\big\}
```

O percentil baixo (em vez da média) dá peso às fachadas mais próximas da
rua e ignora os vãos entre prédios, que fariam o raio só parar no fundo do
lote.

### 8.2 Co-registro: alinhar o OSM às edificações

As vias do OSM e as edificações da Overture não estão perfeitamente
alinhadas — pode haver um deslocamento sistemático de alguns metros numa
região. `coregistration_field`
([`planting_slots/registration.py`](planting_slots/registration.py)) estima
esse deslocamento como um campo de vetores $\mathbf{t}(x, y)$.

**A observação.** Numa rua de mão dupla com prédios dos dois lados, se o eixo
estivesse no meio, as distâncias até a fachada esquerda ($\ell$) e direita
($r$) seriam parecidas. A metade da diferença mede o quanto o eixo está
deslocado na direção da normal:

```math
o = \frac{\ell - r}{2} \;\approx\; \mathbf{n} \cdot \mathbf{t} + \varepsilon
```

onde $\varepsilon$ é a assimetria natural dos recuos dos prédios. Uma única
rua só informa a componente de $\mathbf{t}$ perpendicular a ela; ruas em
direções diferentes, juntas, determinam as duas componentes.

**O ajuste.** Numa grade de células de 250 m, para cada célula reúnem-se as
observações num raio de 400 m (mínimo de 40) e resolve-se um mínimos
quadrados com regularização (*ridge*):

```math
\mathbf{t}^{*} = \arg\min_{\mathbf{t}} \; \sum_i w_i \,(o_i - \mathbf{n}_i \cdot \mathbf{t})^2 + \lambda \lVert \mathbf{t} \rVert^2,
\qquad \lambda = 20
```

cuja solução é

```math
\mathbf{t}^{*} = \Big(\textstyle\sum_i w_i\,\mathbf{n}_i\mathbf{n}_i^{\mathsf T} + \lambda I\Big)^{-1} \textstyle\sum_i w_i\, o_i\, \mathbf{n}_i
```

O termo $\lambda$ puxa o deslocamento para zero onde há poucas observações ou
todas as ruas têm a mesma direção. Os pesos $w_i$ vêm de 5 iterações de
mínimos quadrados reponderados com a função de **Huber**: resíduos até
1,5 m pesam 1; acima disso, $w_i = 1{,}5 / |\text{resíduo}_i|$ — assim uma
rua com recuos muito assimétricos não arrasta o resultado.

**A aplicação.** Cada vértice das vias é transladado pelo vetor da grade
interpolado bilinearmente na sua posição (`apply_shift`). Como o vetor
depende só da posição, vértices compartilhados por várias vias continuam
compartilhados — a topologia da malha viária é preservada.

Os métodos `face_ibge` e `osm_overture` usam as vias co-registradas; os
métodos `osm_eixo`, `osm_calcada` e `osm_calcada_lado` usam o OSM original.

---

## 9. Pós-processamento comum a todos os métodos

### 9.1 Vagas dentro de uma via transversal

Perto de um cruzamento, um ponto deslocado para a calçada da sua rua pode
cair dentro do leito da rua que cruza. `drop_crossing_road_points` testa cada
vaga contra o leito de **todas** as vias, cada uma representada por um
*buffer* de raio $h + 0{,}5$ m em torno do eixo.

Uma vaga dentro do buffer de outra via só é descartada (motivo
`via_transversal`) se a direção dessa via for bem diferente da direção da
via da própria vaga:

```math
\Delta\theta \ge 45°
```

O critério de ângulo evita um falso positivo: o próximo trecho da **mesma**
rua também tem um buffer que se sobrepõe ao ponto perto da emenda, mas segue
na mesma direção. Não se aplica ao `osm_eixo`, que fica no leito de
propósito.

### 9.2 Espaçamento mínimo global

A caminhada garante o espaçamento entre vagas consecutivas da mesma via, mas
não entre vagas de vias diferentes — duas ruas que se encontram numa esquina,
ou as duas calçadas de uma rua estreita, podem gerar vagas próximas.
`enforce_min_separation`
([`planting_slots/allocation.py`](planting_slots/allocation.py)) percorre
todas as vagas, em ordem, e mantém uma vaga só se ela estiver longe o
bastante de todas as que já foram mantidas:

```math
\lVert P_i - P_j \rVert \;\ge\; \max(e_i,\ e_j) - 10^{-6} \qquad \forall j \text{ já mantida}
```

onde $e_i$ é o espaçamento exigido pelo porte da vaga $i$. Usar o maior dos
dois garante o raio livre inteiro da árvore maior.

Testar todos os pares seria $O(n^2)$. Em vez disso, o plano é dividido numa
grade de células de lado $\max(e)$, e cada vaga só é comparada com as das
9 células ao seu redor (3 × 3) — qualquer vaga mais distante que isso já está
fora do raio. Isso deixa o custo próximo de $O(n)$.

As vagas removidas aqui viram vagas inviáveis com motivo
`espacamento_minimo`. Graças ao passo por corda (seção 6.1), elas são quase
sempre vagas de ruas diferentes perto de esquinas.

### 9.3 Metadados do IBGE

Nos métodos que não partem da face (todos menos `face_ibge`), cada vaga
recebe setor, quadra, face e, quando o OSM não tem nome para a rua, o
logradouro do IBGE — da face mais próxima a até 25 m
(`enrich_with_ibge`). É só metadado: **nunca move o ponto**.

---

## 10. Canteiros centrais

Só no método `face_ibge`. Para cada par de pistas de uma via dupla (seção 3):

1. Amostra-se a pista A a cada 1 m: pontos $A_k$. Para cada um, acha-se o
   ponto mais próximo $B_k$ na pista B.
2. O candidato é o **ponto médio** $C_k = (A_k + B_k)/2$.
3. A largura útil do canteiro é a distância entre as pistas menos as
   meias-larguras de pior caso das duas:

   ```math
   w_k = \lVert A_k - B_k \rVert - (h_A + u) - (h_B + u)
   ```

   onde $u$ é a incerteza de posição do OSM, medida no próprio município
   comparando o eixo co-registrado com o eixo implícito nas faces do IBGE
   (percentil 90 das discrepâncias por rua — `ibge_registration_uncertainty`).
4. O candidato só vale se $w_k \ge 1$ m, se estiver em área urbana e fora de
   todas as zonas de exclusão: os leitos (de pior caso) das vias, discos de
   esquina em cada cruzamento e os raios dos obstáculos. O disco de esquina
   tem raio

   ```math
   \rho = \sqrt{(h + 6)^2 + (h + 1)^2}
   ```

   com $h$ a maior meia-largura entre as vias do cruzamento — o que mantém a
   cova a 6 m do meio-fio da via que cruza.
5. Sobre os candidatos válidos roda uma caminhada gulosa simples
   (`greedy_walk`), com o porte dado pela largura do canteiro: até 2 m
   pequeno, até 3,5 m médio, acima disso grande.

---

## 11. Resumo dos parâmetros

| Parâmetro | Valor | Onde |
|---|---|---|
| Afastamento de esquina | 6 m | `CORNER_SETBACK_M` |
| Recuo a partir do meio-fio | 0,5 m | `CURB_SETBACK_M` |
| Espaçamento `10m` | 10 m | `SPACINGS` |
| Espaçamento `porte` | pequeno 6 m · médio 8 m · grande 11 m | `SPACINGS` |
| Porte por classe de via | `living_street` pequeno; `residential`, `unclassified`, `tertiary` médio; `secondary`, `primary` grande | `PORTE_BY_CLASS` |
| Raios dos obstáculos | travessia 3 m · garagem 2 m · ônibus 12 m · árvore 4 m | `OBSTACLES` |
| Distância máx. obstáculo → via | 8 m | `OBSTACLE_TO_FACE_M` |
| Passo da busca por corda | 0,1 m | `ADVANCE_STEP_M` |
| Ângulo mínimo de via transversal | 45° | `CROSSING_ANGLE_MIN_DEG` |
| Pareamento face ↔ via | ≤ 15 m e ≤ 30° | `FACE_STREET_MATCH_M`, `FACE_STREET_MAX_ANGLE_DEG` |
| Raios de fachada | a cada 5 m, até 40 m, P25 em ±15 m | `planting_slots/buildings.py` |
| Co-registro | células de 250 m, raio de 400 m, λ = 20, Huber 1,5 m | `planting_slots/registration.py` |
| Largura mínima de canteiro | 1 m | `MIN_MEDIAN_WIDTH_M` |

As constantes ficam no topo de `scripts/03_allocate_slots.py`, salvo
indicação. Ao mudar uma delas, rode de novo os passos 03 e 04 de cada
município (ver [README da raiz](../README.md#estender)).
