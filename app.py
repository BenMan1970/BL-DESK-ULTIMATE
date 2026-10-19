"""BLUESTAR DESK - Decision Intelligence.

Un seul bouton : la chaine complete s'execute et le desk s'affiche.

    Lancer le desk  ->  4 scanners + calendrier  ->  merge  ->  BLS V10 Pro

Les 7 programmes d'origine sont utilises tels quels, sans une seule
modification. Ce fichier ne fait que les enchainer.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import io
import time
from pathlib import Path

import streamlit as st

from bluestar_chain import ETAPES, executer_chaine, lire_identifiants

# ---------------------------------------------------------------------------
# Logo BLUESTAR, integre au fichier
#
# Pourquoi en clair plutot que dans un fichier image : les envois d'images
# vers le depot n'aboutissaient pas, et l'interface affichait alors un « B »
# de repli. En le portant ici, le logo voyage avec app.py : un simple
# copier-coller du fichier suffit a l'installer.
#
# C'est la meme image que logo.ico, reduite a 128x128 (fidele au pixel
# pres : ecart mesure 0,00 %). Elle s'affiche en 52 px, donc 128 px couvre
# aussi les ecrans haute densite.
# ---------------------------------------------------------------------------
_LOGO_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAABYpklEQVR42u29Z7wkV3Xu/V+7qjqe"
    "PDknzYxGAeWckYRIsjFgMhhMNNjXAQd8fV/7Gmxf2ziBjTHYYANOiIwByYBIkgDliLJGk/PJ53Ss"
    "2uv9sKu7q6qrz4j3/Xrn9zvSnDl9uqtq773Cs571LAGjAIgA7q+IgMZ/fy5/Oi+VxDcqiX9b+vdS"
    "Hyc/7efl/Exyvtfk9Un++yRfy4DPSP7OoN/nOVxX6gOe27MW4uckz+W1Er/r0u9tct/M/hSL/9Mu"
    "miYfXPyNZi9U+/ZT6ncl84+a/H/mlzpPTRKv6V6D9j//5ANO/UyX3nSp12vO7ejAByOD3i/zd13i"
    "WQuD30Ryf0nin4jRn/6Ey+CfdU9++klK9wFJZgFPclSSr+87zXknSAET/zW5aINOmuS8ceb/3Q20"
    "lBWQ/oVPPIPkW6gk3k7JP6ed985+9kDLdvI7ytsy0nMBP6W51YxNSmwOEQMiqLWgEf/3z/+PP2JA"
    "PARF1SasiaRdD4M3wklcxQALkLfDUqY7eToFMcYdABumLsAPfAqVIfzyMIjpHQrtmTRBEiGH5lx9"
    "vFulc5q03/aJICK9/Rj/XRXEM/EJxV2nOP/Ye43ixfdkFVCLWtszVBJ/ZsdjqPYMnqpbnKT1c9+l"
    "wwNNBDypR6rdQ66q7ufW0m4sEDUX0bBNlLIqXhwz6dKxynP23ifbAClf22/eRUx8Me6kV0fGWHXG"
    "Zax63uUs334BlRWbMKUxMCUUgxi3slGoWNwNGwzW2q7JNsbEi6Hxx4p70CLxAsWhCooYDyvGLbrv"
    "Y1XifxcsEKmAAStgcf8PPbA+WAORdbdTMqBtwIIHSOT+XXA20lOFsA2hjb2nxYYWtYpnnCW0NooP"
    "gcVai40UMQZBaUcRGtt+ERd8WRthbXyf1j0P9wgsGtYJm4vUju9h/tl7WdxzN7Vn76IxPxsfHJOO"
    "nn+agDS9b4ym/PfA054+lSICYronfv1Zl7D9+l9g7YU3IuVVzB6fZmb/bmYPPsni5GHCeh1shIri"
    "HrHFWkU0jE+iu3ljjLu5jvsTcYsZRe71xuudKPFQ8cAYEB+M+14B8XwwPvieO2UmAD9w/w88vEKA"
    "MQarirUhtt1CGy0IIwyKiUKIQlBFrcVo5L63brOrRmBDBOu+NHT/Fl+4jaLYGvUCXcX2/Lra+B4M"
    "ILExtfEDtgTFIn5lAjOxGX/FNqQ8TFibpvXsbdQe+hKzz9yPBXff2ESsICcPUiW7AeKT2BetJGx1"
    "J35DBfE8NGoDsOGsizn3jb/PxOkvYPboNE/d/t8cvOebzO5/mGjhGIT1XrSd9Gu519rZ0YnrIRNo"
    "pW5SegFf50TgfKb73vReK378ZeIb8RL3F7kjT+wziNy/qY3/3rNwdBfJLaJ7vY3/PScb6N5DYoHi"
    "2En6Iz7n5zvPQS3G8/FKwxQmtlDedgnB1isJxlbR3vtDFn70ceYPPBq/p4mvQ36qVFS6hq5vdyRP"
    "fmIjeD4atamOjnHx297PqS/9FQ4+tZ97P/dxDt7zX9iFQ2AsUihijO8WI/Vx8dcgE6V60iRHRFAk"
    "jgk6i95JqAzaWfz4dYhxriqR/qQ3ZGfzARohnUVFwVrAIl1f7/7uFr93Ynvpn5Ib18cbW0TygzTN"
    "bK7OP1uL2hANWxC18cuj+BsvpnTmKyiOrKb16OeYvedThM2Gs3jW5ryxJNLh9LN3G0AGASOaSnGM"
    "8bFRmzW7zuf63/83bGkjd376Qzz17U9D/QhSKmD8Us9Eq/Yn1pKz6p2Lytx8d7FyXuvWPrkB4sXu"
    "nIb4e8WAMUj3ZMQbBO3L3ETjrCUOTOl83z3d7h3d6QwTi53dANqXh7sYRnsbrS8+z2yexAJqJ5EW"
    "F2Rru4YpDFPYei3lM14BC/uZv+0vaM0ccJugmy1kY4LE50sXg8i6gITv18RuNR5q2+y49lVc9b5P"
    "sfeBx7ntQ++lfvh+vEoJTNGlfQMAE1XJLFrO6VsyaZHU6yW1mSRe1N5mSJ7+3qmP45aOVbPxfUtv"
    "4USjBI6mvVOpSZPfsRKaRrg6r8mxAD0cSlOHS7q7UBMupvc6IQ6ysYm43UC8EbyxrRTOfjOFoQkW"
    "f/iXNI/+BIk3gQ7K3BKbzgP+d+p0Jf2/pBf/eS99K1f97me4/6tf5Ad/9W7C2l786jBqjbtwkaUT"
    "DlnKCpwMfMhgr5J4PzLXbZKbJPkatzmMEbQdsnnLGgrFgMWZOYwL5VOQY8/UuwfotpSN/bf2Qa/p"
    "25KM/0+afk2hnZpCAzXlqnrvm7Ao6sAuCYpofYrw4N1IaTmlHTdiZ/cSLRxGxEtsRslNq+lFUBlA"
    "PnFIxTOobbPzmp/nsvf+Ez/8zCe56+9/Fc/M4xWq2DBKB22dTdT9Si4WPZOZNfd5MO4gfLQbWUsC"
    "0ZJeAKQ9n5tyQ4kNzeIi17z4elacdw7Mz7hMIoV2Jr9sL2rPg27TOHXOJpD0KUw8YM0i9hJv3M4G"
    "podrpM+uRaMIvAJqmzQe+jTtQ/dQOecdBONbUQ0T1i/9rJLrY3onUvvuR4xBo5A1p13AVe/7DHd9"
    "7l958FO/i1f2UCm4tKezwFk/nfLfkhvFp53Bc60GZR9eTl0gifMbDzwfvF6GYVUxBeUF111N4cxL"
    "wC52T7VohIhxZjSZRagO3Jwdq6C5r0kEYJo449JJpVOQUNb4ZoKU3kHqek61iPggQvOJLxIefZjK"
    "mW/ClCdiq9x5ZJqB5zVRDFJ6OyR+eGIclFsemeD57/t3nrnrLu795O/il33nOXIgXlnqEMugckXS"
    "vy0R/OUGSz00Tm0iYPU8dyv1GsxOIVPHYXERTAHximirydYta9m8fTtTq87DrFoBzYUYN6hCqwUz"
    "x2F+Elr12FZ6sZXppGp5MQDpdBAd+EAS0UvquWnq9pbKiBIbRy0qBhu1aTz1NbQxT2Xri9PFh1QM"
    "1dtUfi9Hl4yPENCIC9/yfto6zG1/9xrEtFEpuwhY+s+vpBZa80vCmWAv9TdZor4l+UZCsc6TGeOe"
    "XGMeWk2oTiBbT0N2nodu24G/ZRn24Ar4tz+Do7dx9WtuYO1EhcUgwJx6JtFt34ahbciLfx9OWcDu"
    "O4zsfgaz53700ONQO4r6JShUHOBkw24cYLuLmJPbdvL6jLvTVCDY7wLT7yS5VSNJvloVJEBbszSf"
    "vYXC5hdQWHk2raP3x6l4omoqvdqNn/eQJUb41j3vMrZc/Ra+/eH30Zp8Bn94HBuFA4M9zfo/yalO"
    "51XNTlqF0q4/VDImXnywbVichsIIbL8Ezrke3X4GUjYwuQ/2PIG965vogcPI4nEQuPLaS2j7wLyH"
    "7rwMfnALNPbB138TXb0KWXsGZv1p6M7r0IaHHHoYnv4WuvdOJJyDwjB4BbBh93q0mz5qX/CrnbpJ"
    "stQh2fRWE/ebjZkGxRrJ2M2CV6A9tx9v6nFKK55HOPUktl1L4C+aKqn3cIBEpNp51C98/9dotqt8"
    "54Ovxy94fbBzrnlX7QdakuXgbDHnOeLW6SsDMQFoiNanYWgdetYrMWdfjxYF3XMPPPIt2Pc01OZc"
    "ddIPkMAQicfKiXFu+dF/0S4v40X/tMisfQb+5KVEYRGDhXYMAwMUS7ByJ2x/EbruPIiAp7+DPPpZ"
    "ZOEAlEbBBG4T9uHo7mQmM+xeJdxmjIJm6AmaEx8lClEd26P9B0ZtiF+eoLjuEtqTT9A4cm+MgCYr"
    "uO7vfhr10zjlC1lz5mVUN1/AXX/xHsQ2QEYSIElnESUHpcsrIGriDA/y6yyZRnZNXQzz2voJpLIC"
    "rvwDOPWFMPUY9lsfRp69C2ksQlByizc66m7eWownRNOzXH7DlSxbuYJnTjQxzTrR8CkUN24n+slP"
    "YGgMvKB3GNSiRx6BfXcihSFYfxG663XYG18Je25GHvoYUj+GFCcykLD2ocAygPHRcQedtFVOYiDz"
    "Q4QenCzGJ6xP4i8cwh9ejZksY9v1fgheBZMOK3t/Nl3xWo499RiTT/4IU6q6wCcZ3YtxqUkm62PJ"
    "hc7P7rOEDRkUABkfogY0JpHT34L+7FfQoQ3w1Xcjn3s78vRt4HswMg6lEiKKhG0kbCJxpQ4bccm1"
    "V1AHCtZiGotQL+CdfhUSLsbwb4hEbcSGoBYJijC0DIIA2X8b8o1fxNz6bmR4A/qSL6M734i2piFq"
    "uqxDNRPO9gJGTeb5KWxA8r1n7rPTfCQxAz2H84cAg19dlX7yvTp8AgeIT6DakMrYcpbtuIy9d94M"
    "Ud2BChmsfulcuN81yBLJXOdiZMnFD6BxHKrr0Bs+h279Wfj++5BvvB1OPAnVZVCquhMYhQ7RS6J4"
    "WMJ2i7HlY5x/9ZVMK7SNcWjg9AJ2y5VIpYSG7a7PlU4QpxFiI8RaKFShshyZeRLzzbcht78Xtr8S"
    "e91/YiuroT7Z2wTaj3OIxgBTKu8HTDolXJJDmc22swyt2JJH9Uk0auKVl8Wxgu0rupnUbo1NxMod"
    "FyGmwvEnfoj4fg/X10zqpdrd05pXxNHMomqCBdK31JpIazLpjnhQPwpbX41e/1k4ehd87VXI0buQ"
    "ygokKCHadjl8B27IXI9nBK3VuODcM6ls2cRMwxKKIGEI89NEo6diNp0CzYUYgrGgvUhfurUDi9i2"
    "CwArK5Fj9yJf+znkyJ3odTcRbX4FWjsRs3gG1D26hack46TnCtNcD+36/oz96LelmmZUatQias5g"
    "ClVModyflqqks4DO9QxvPpuZQ3tpTB7A+MHgClc3vtc0gVNyuATZyKDrY6UvNuk5TuehtDWJnv2H"
    "6IaXIt99J3L8LqiscG9vW6m6Zi+9Mr3Ylrju3m5z4XVXM+cLjXpEUYFGExZDovo4Zvsl8OjDUBnp"
    "VdW6JrwfgME2Eb/iPvvBP0UPfQd7+T9il52F3P2/MKUxB/ZkeX3Jm9Wc4mzGcEg2RdQehJy/Lgku"
    "YnMOUxzCC6rYVq2P12HSDFhXCKmsPIWpQ89gW4sOEUvljtKDJiXh9UVyPVM//qwDkkfJIMgGwaKt"
    "OewlH0WXnYf894uR6YehsipOv8J+v5ZECbuYvRBGlqHRYZ539RVMNqGOYTFSaLag3UKPTaHrL4Wi"
    "B1GUwBnz/HnikzR011JejUw/grn5Bpg4E73yY2i7FpeTZWl/PdA9SuoZSX/oOKBeJt3Ss7YWQCOM"
    "X86tQZv0w1M838crj7B4/KC7MRL1+z6ygebU+vMhzP5nkEPj7kaU8UkI69hL/xEoI999NUIbgiGw"
    "rYTr61XV0nl1YuE8g63VOPvMnZROPY2jC5Z5FZpWoVmHRh2mTqBDp8HqDWirHvMX+7e05jQOCOLc"
    "QjCMaAtz66sQU0Cv/ARRu57dMnHFsle1XApSlbzoSpaAzFPhgCEKG2AjF8j2mSHBZDeSFxRRC/W5"
    "EzmHVgcsIJn6VbrOtPSml2420Svnemhzjuj8D0FkMXe8HRNUEOM5X58LGHeqdf1MSRGBZpPzX3At"
    "x8oe862QWSssWkUaNWg2oDGPbY8iOy+G5nzmNCU9WQabT6KtNkTFQ4Ih+ME7UG3ClX+Pbc3HC/7/"
    "jbipWYLOIKuaweI7MYtNYhSZ7N30B9slNNI4b0xUsYScTg0ZnLalsho9SYNQwnSLjzan0DN+H7wR"
    "zJ2/jJRG4xdG/VYkWcvS9Ebs/AmtpTpcYdvVV7N3AWoqzIbQUDBhE6KWY4TOzCPrroCCyWEmaYbG"
    "1Vfoj2suLtI2pVHMbe9GpYie/4docxokyDnPOmDFkyEfufWFPmzB5tQL1BJFYZeqnw0CTC4PHcV4"
    "OdW87kd2c5rc4qeeNF3M4wE6dE9aU+imV6PLLsLc8z+QoBq/i03ECY6ImftWmczCGIOtNzl151Z0"
    "5xlMzis1a1iwULcg7SZ0vmaPYatnohNrodVAjGRbW/pTXk0HitIhd6pCUEXueA+MPQ+77Q3QnHRY"
    "Rqq6mCyV6xJBVKZcP6hmpv2sKkdy0X4sQPI2gLVE1nZRI5ElWtqQvr0s2Tw1Gw7kBDvOfxs0qmFH"
    "TkdP+SXk/t8EbcUQZpQBRyS/WBQziJOAlRgPGg3OuvoqJoeLNNohixZq6iwArYar+IUtaMyjURU2"
    "nYNtLsYhkiZKvYMLwv1FLwviYYxifvxr6LbXE40/D9uuOSZzzJIajPxIn8FNlo8llTKfLK6UuGim"
    "fRvB9N2EicFBa/siezlJoTa3PBDfm0kmEZLnPBxPXnf9T+Tpf0Tmn3KnX8PY0mTfQNI0Ns0z/kLb"
    "KuVSke3XX8uhOrQt1CNoRNBWi4R1d/pt5FLK+TpsuRo1EVg7oMafyFmkRzXLVuqMRmDKUNuPPPr3"
    "6On/I3YDiorBiOTwYHVgYVxT3ITk7w0GkBxeEyVyrLS1MH0fnKJla4bGlIl8RTLp4BJWPruVEl0+"
    "io+2ZmHz65HWcWT/F6E4AVErsaSaE1skWDMJ+99NLI3BNlvs2LkVzjyP2QUlxNDGnf5G5HJ5opaj"
    "r9vIsYNGz4TxVWi7kdnQsiQ9JU1P63SvtKEwjhy8Geb3YTe/ElrzLtBNcgo0L5NKMKqyi78kpa4/"
    "GMx1mUiOC+hwzqQ/J+2FJUoyTEmbR+22O2XQyXSw14mcjQHbQsvr0XUvgKc/Cn4JIYqDQpODNEt3"
    "obVz+qR/WYznQ63OOVdfwdRohWYrpI2hFfv/CEHaNWgtQhhC2ITGFDCObDwfbS72WEFKwupIigGg"
    "6Uy6i2R2/s1ohATDyJOfhGXnoMNb3T0jqEofh7iziTo5zeBOfc2tC/RFASIDf8/knta4CNLZcR3Y"
    "Nw3lksC7Nd8Iac/USC4wGnf0RHXY+HI48j1Y3Aum2EMCk3lE3PjhFt7LkFgSrOD4/20rFItFtlx7"
    "DceaDvVqRErTCm0LjbZ12H/Ugs5GaNfQWohuvhyk3fXTmqW2d+KW7mYwvU2dAMs6BwfjI80TcPQ2"
    "dP2L3Wd27iHHPGczbx0YRfXHIH3NXUI+Vc1ZAOn7Z4PtpjPpGEt6NOb0I8nyWJck96QWyjbRoW3o"
    "8CbkwNcRf8iZq07LVIfb3wFPjIlrFok+gM6mSHYFGR/bDNm2bTPeWRcwt6CoGiILoXU9ge1IUdty"
    "vr9dg3bdgV8zx9CJM9DRlRC2XG9iEgNMUc1NLwbodBuJoHiJ6xPHn/QryOHvoaaEHdqERq0UEJRC"
    "MVQy8Zos8Szpy9IkY9El23LfQwJzet8T/WvdBoocH6h59Uvpi1wSHyi9VLPD0Y9a6OorYepBaM24"
    "ql8XEEq2efkuVuj0A4oXb9WYo4jX7RVU8RG/APUG5155CQvLRmjXQ1oW1zxqoR3hum6jlivjqnX+"
    "OmxCYxrx1yGbz0Zbi72Tqr2Npt0FT/4/LlyJFzOfJL1RxUNsAzl+N0yc43oMYzemGUgr6f9TD1N1"
    "ACG2F7910MpUd3M2Qo2xHZPN2RSIosh1uvZVrTI5aV5/m+oS3kq7phIMqhFaWgnFFciR28CvOLpz"
    "vPhuMYPul5UAxX1ZKWC9uCHFFBxLWYKY5eYTWQ/P89l27VUcb4GxQhif+ihycH+7re702xDChlv8"
    "sOlO/WKEbr7CXSNefE2+A6rw3XUYHzWu4VTx3PfS25C9zRr3JQJ4RZh9HPWGoLgsPmSZXkZlELqS"
    "DwJoFkLQ/JQyB8Tz+4maGufTZrCV6QvINU1VygjZqPTIxz1WiocQIcPb0IXdEM64ypraru90/Xwm"
    "FYZK5+RJoh7WtTC2C6zYRpNtm9dSPe8SpufAiiGMuq1+bhN0LEBowYubRv1q7AaOwrqLkJEVEAn4"
    "RTA+hkSPoSROmU+CMZxelV5bWIdfECKLe6A4joQLjoGkYY9I23kftT1hCHrtYWSetWbSvmyxKH8/"
    "aXzZmp/fdv24SNqEa7qc2wUjJF0v6/zXArbRcE9bvPglBoicuT/+DNgnHddOmqBJUyo9E6sd11Ho"
    "Y7WA6fXw2Tb4ArVZzrjxak6sGKN1IKJtnbaATfSLRFZdl3NkY3EA3/0/asLiJOKdhV1/Ojz8HfBX"
    "u2sTPzb5ngM3liqQSYeEEXcZd1IxUag96O5RBWj16GSd13sgfuByeO2vAcoAeDifdWZz8Hm30fx8"
    "fp51XPMlcOjkhkjvxnj5O/12nsfa7TtZM1SmbRuEGmAigxEf4xUwBoJiCT8oEXiGoFDGBCX8oIgX"
    "FBG/iPELGK+MH1TRQhmCIr4neH6AiBeHFBZjHJFDPCVs1Bi/7BweOapEkdCMhChSwji0aavjfrqe"
    "futOfRTHAKaBeHXsnn2suOYt7Lj8fIxfIUDx1WDiPgRjFQlj3QBtOxwhsoRhmzBsY9shUdQiDEMn"
    "BqGWMGw57YCwBZHFakQ7jAhR8A2+RIgUODi5yPzRfQ5MSiGtGidjms+rTDJxOlmbtf14Q8Jw5QEB"
    "/UmbZvjMHWMggsTiDp32JU2kixq2wfPZdvUbOe/ii9h66hAjG0bQcon5CMKmopHFSCwO4QlRbARC"
    "iRdKoR1fQiv+srh/a4Xu55E6S95WaITuNQ9Nw7O7LWuWuYuNrHudWqWt0IpiQkmkEHnucYRN8EI0"
    "rMPMXma3v5pHT3kFPlCuQLkAlaJTFCkYZ2ziy3bPIP4qdNbAanywnZuMIksYKcZ4BCUP34eSVSr1"
    "GYKjk7SfOcbB+37Mwdv/FWktIr6XqgammVeaowOkXZJO2mgnXpuoCfknYR/mkVh7vqpbApO+Zsnu"
    "xYjh0MN3c9MDt3PT+uczsv2F7Fw7zqnPG+PMi7ax9tRN+Msq1A0s1KG9aIlCt+sjI4RWaKnSttCM"
    "6JrxlkIzjua7GyD2663Y11dLhpEKHDimrF7uHk1HdsaqCwitjRJ+wcYQuEJtGs6/nnBimJnjdcQY"
    "CvOGQgCVAAJfMOLkYbw4WndqI6Twehu5DwvFwwYeUvAoBzDUalPat4/wmWeZe+oAM/tmmDy4n2jP"
    "LdB4BCggpaF8ql0fMtEB3pIc7Cy4lN+E4+fx8t1updcLn+sMbNr8ywCWilq8yjASFYgO3cLc5F3c"
    "veI67r7vDIJP38W6iYhdu9Zw2iW72HH+dsY3r8NOBNTbsFgHbUYU1HH6PE/iky4YC2IT0iU29rQ2"
    "LnAYaLaVVaMwNQ/HZoSJEVArbp3FIYG2A3p1VECMcVDtyp2w6ywMEV5QQAwEPhR9KHhC4Cu+MXim"
    "F+/E5XenD+TIFVCGIAA/VIrTR+DRp1l87AlmntzN5P7j1MIykXgw9wBM3YmxDfCHXPbQMSkkW2sk"
    "wxDQgShwj75v05YhYQb8rFqn0lHOsjkUonQTQ7ckL2nxib5KlFosAoVlSLuOHPwcsvwJ2quvZM9U"
    "mT1fv5eb/+vHDA0VOXX7Ks64aCc7LjmDlWedCsuqLFiYq4E0HIqjcYZgRFx0bdWtqGoMXQhqlUgh"
    "MMLGlfDoXsUYoVqITbKBRqRENnQbKGo5BDIKoTQMp16OlLWr3KXdbN6JUCmGMP68QECjCItBCx4U"
    "PGclFuvY3U/S/snDzD/yE559+gC1mQZtU4TyiGsqaT6DOf4jtHYElRLWlOPG3Bh0y/b399lxUqe/"
    "9xMl1faXDQDjNcyNAdSGCdOjS7gIycQV0mMxpq5XMMZDNUS9gsvjjz0Cxx9G1lyKWXM1tl1jYf4o"
    "99z3JPfc8SB+8Yts3LiMM8/fwa7Lz2bthWeh69aw6Bnm69BYsLTbEUZMnJolT6FFVTAitCMo+8ra"
    "5c4VMA6+UZf6W7Ads0/RHd+wDutvgFXLoeAWQrus7YTzjHmDofGwJY/SiEcJ8KdOwMP3s3jfPRx5"
    "4GGm9x+nUW+BX4ZyFaoVpDTm+hsOfgNmH3eHw1Ril6kJvaNE6Jcn3zOwJyBLNM3hEWqqMygZ42mi"
    "dzC/RSXp8TsPiCx/pfusDCJx966J8QAxSGHYoYAHbyeaeQrWPR+prkDEImUIo4jdew+z+7Gn+Mpn"
    "vsaK5UOcds4ZbL/qPNZdei5rtm5nfjzgeM1ZbGujONsx3dTVRs4KtCNYMSLM1+DEjDI27K4tVOmm"
    "jirGgUErd8GmU2HYIL6Js1bB9wUPG5eIfWzRo1iBsoXgyLOEP7iT2XvuZvKhh5k/Nkk7FCiUoDwE"
    "oyOI+GhQRoIKTD2IHr/XbQJT6pFEiLpwd15vxWAVlQQlSqXPFSexhPwYIIc+4CRGMsphCZMk2Z2T"
    "z0rOFFB8sHWobnHvPf0wFEehPg1P/ge6/Ex09RWuu7d9DCkYTHEYa1scX1jk+9/8Ad//+rcZHquw"
    "6/QdbL3iAlZffimjO09ndqTAVN11eTs5tx5LUESIQlg7AXMLMDsP1RFohJYI6/CIqAmltbDj+bB2"
    "DFP1YmZPp2TqYcs+hSEYshHlg48Q3n8bM3fdxtQTzzA/OYeK7xa8NOSANOP10M9gBNpz6JHvQu0Q"
    "SBG8sstChrZBew6ax3uw81ICwkpfm3dKYS2bsiupxtNuZTMfB6ArFi2Spi4lF7uf0CtZgnFfTdqh"
    "eta1c9k2Mv2Q+6lfBBvAiYdh7llYfzWMbkEXDhG1Zl0K4wfIsKIUmQ8j7rrzfu76wZ2Uq59g8ykb"
    "2Hr5xay/8nKiM89jdkWFmTqEiyDtsHvDvggbVglPHQDTgErsz1GL+FXYfBW6cTUy5qEeWN9Axacw"
    "AiPtNtVD98P932Hh7u+z7/Hd1GbnwS850z4yGvtsL1Eq8yAYgaAI0w/A1KPueZhKeoELI+D5SPN4"
    "z0r2tXINMPdZvWLNiAJqjiVJ/PEHC8ra3NampAae9LdMxOYm7017ApAMrYHFQ7FWtR/j7UAw7E7v"
    "7v+C8e2w+ioojsD8AVTbqPoxVd1iKhUYKlOPIh57fDeP3f8o5X/8DOu3bmTTZRex4oorCc68gIVl"
    "I0y3obEA0goZDoSVI3BoFobKHSnAOiw/Cz3lHJgoo5UixTEYti1KB36EfO1btO79Liee3k19YRG8"
    "qgMFxsZ7xOowRL0gFqfwXOdQaRk0TsDRe6A57cx9twcuNvVW3evaxURZWzPuXnPScAYUg3JayYz0"
    "2vKTRFYZFARmuV3a74ckjxnbLZWq6/jpa3N1O1TKQ2hYdq/1AocKG+ePrfjgjcDMHpjbB2svh2W7"
    "YPYANGcdZm8FG4Y9S1UuIZUydWt5avd+nnr0SYqf+k/WbV7HpgvPZ9nV11A4+xIWlo8x1YRxYKam"
    "zM5bVw4ubULPuAHvnE1Uyw0qR+/A+/a3aN93KyeefIpGowZShUoVRpbHG9+pjDnxDc/1A3YqmcUR"
    "xATo5D0w+3SsYlrpSbR0SSyx7m9p3G2YTh1En4NO0pKBH2mJv2xnVsKF+ANZqJ2G0JTWn6bxflmi"
    "mSHPCMQ3p5URJBqNNW8MEpdP1UqsqWchqLi8fP+tMPM4rLnGdQTN7o5VqL24Jz8mrMQdOFIuIeUy"
    "TQ1dEPnkZyn855dYvWkD6887m/Erns+asy+luG2CZ5421Mtr8K58N6UNc/jf+V/IQ99n5unH3KJT"
    "dg2nQ0NdVA9rUeN3y89uceN2ci9ASsugcRidfBDaC44TCLEou9dj58SEEhQoL+uKtvfJ9fUtaP+h"
    "6oZ92c2TLPyo5gZofi9TkF6dJxY1UE5OPJOkWRrUC5mkrHcEE0qjMfXMxEUV47yESq8iJkBhFBaP"
    "w9OfhRXnwfILYGES6kfAL8SlvbhNTC1KFKewHlIuI0NDtIB9R46z76YvEnz+S6xYs4z1F13GKRe+"
    "iLsrRcz9f0DzC3ezuDANDEOxDNXh+FnHi97hICTLu8bpE4OBwriz3pP3ovN7HPnTq8an3sTeL9le"
    "H/MhRKA8Dq2pHF61Dq4KJ/ABzXMHmU5jydD50xtAiUWcO2sUOvWpJUJ7kSVK1n0kSU1vXNtAq8tj"
    "2RQTW4GO+lsCZrauaqh+2eXqR38Mc7thw7UwdA7M7HaRnh84WrfEFUFDgk3kijxSrCAja2lXV3PI"
    "X8WhuQvxPvMvREefJTrxBARVqK5LsXt7ni/JNEr4eAW8ClIcQ2sHYfphNGq6U9+lQpsMWbRbRInL"
    "3b6zAJMPJ15rnNzLICuQPY4yALBNWo0UPNzL7vw8B2Ajp5ffx049Cfc873vN+B7Fc10yK0+JOX+S"
    "EVVQRGyimipdEUo1Y9Ccg6c+CyvOhlXXQH0RFvY5RRClJ+EaNd3HFsahuh4dOwUd2QClCWT1drz6"
    "dwnvuB1WXACnbIb9d8TaAjFXzwSJE59gKJlCfOqB0goXyJ2426V2pghe7OuNoV+sMsnyiQNiv+i0"
    "DZpzXSZRmsJnl1Bd04Syj/S37iXjdskZpCQJF5DF7/U5qLY+J036pNg06mTcF46g1XHEKyYElHBC"
    "FLZ3w2roBZRqnFxLUAItwbGHYGY3suGFsOo89PiTYBfcwhXGoboShrfAyAaoLndmPTCwbB3iPUX0"
    "+Q+AlGDxEIxfAct3wtRuJ/4UxSIRGsVGsuCie+O7hYs8ZGitO/UzD6O27cw97tSLpGXZ3EnXhDWJ"
    "eYOq7vNK40h9yrmNLAOrjxeofYJhqjogZaSHImZrh/HO6SsGdcmfxqTbozXf3Pc0iAepZKZvSLwC"
    "OnsASsvQ8rhLwZJCk93T0cEPfFd4EgfgS4ctUxx3QeAzn0dXXk9w4ZuJJo9hy8ugMOGUPApFV70Z"
    "rsCQQ+UKYxH2Y/+TsBa6/F0VZg7BqvOhPuMW3w96aXCHpBGFzpf7I8hZb8A+8GWYvtdF934Q5/cm"
    "3T2Xyrw60pwJDeOoBWObkELVgWF+EBNiJS22lQrEJVPb1yVO6Ul+pnkWQFyB20XVeSdaU+UGVDJ7"
    "I1uUSLNZ1SvA3FEQi45vQI48BIWqI2bEU0E6D0B6iWxMyOy1rNEZGEEJM/M4o2dtYHL3WdCed/tn"
    "yOHuVArIcAENAoKVw3if/RVaTz0FIxOxtbEQzrla85Yb4Omvxua/EDN2tNvmBT6qo7BjMzxYB288"
    "zvvjEjIJqfaOGzNJ1ayeEoKKcW5q5U5o19HGNFIMEsMksmMb8gq8eQc+i/hpgliqfZvA5KWXPU3c"
    "dOdPlpgkOR6//1WaJiYaH2nMwuxBWHNWHLxlu3u0N5GkmzPH8q3Gd9KoJkD8MkTCxK4rGRvZgVpF"
    "Vi2HTetg4ypYO46sGoahAG/NMJWH/5XmrV+BkWXxcLFEqtuYgcJyWH0OtBsO4TOdqSNlB1IVx6Bt"
    "MS0P78wXQxQ4SxPzBcXznRsTcUwl8TJ5f1LN3AOryIbzHeZhGy4+SqGtAzKCARMBJUsaFVjSfHdZ"
    "wdJPCRPJ6lcnullJyxXKIEukmtNg6kSS5Mi96PYLE45MstWIBMQcb8IONiGeC8b8IsgYmy68gQV/"
    "DDauQNdMwPIhZKSADPt4VQ9/5TBj8w9R/9SfYosjccpqEjxD4wCm+SOw8kJYtsMxgzrBoFeMsfsS"
    "lEpw4ACF510DwRanQyxBbI28HpXdOM6gqwmYdENJXLGkUIEVZ6H7702JVWvqKWhGGkAH6hJrbl9Q"
    "uoHnObWGGRIdLX29SZrqyxuEI2k2BuhsBhtBoYw8cwcs24IOreiKMkrePMJU5ByfWuM2gEpAYXgz"
    "6y66hKmKgdUlZNTHG/UxVZ9CWfArHuNDdcJP/i9acw0k8BI07MTAiQ5xotGETdc7jMGq22hScAut"
    "AZSq2EMnKK/ZgFlzjrMCEoD4saxNZzBWB9ZNLH73ecXmf2IzWloDR+6GoJRqdUmyrftmbS6FEib7"
    "OvoOvyQOm8bNodqfv2tKXm0J+DHnwGvWHGmCxNh5QVCCI0/A4hR6yhVoYz4+Af3Ah6a6g8SZfy9A"
    "vABawsZd59PYup6WtAhGfLwhH6/sU6wYCkWhuipAP/9BZu97EBkedhmH58exToev3/GXgUPvImDj"
    "1TGm4Pewey9A/CI6O4PfUMbOewG0iu5apMMU9tLNr4nj0tkQajxo1pDTX+To4bPPuA2tiYEbJDuT"
    "sy43r9N3cC1gUCs9kG8B3Pgz29dmnNfqIQnmSZ9iheSBSHGaZ0N4/Jtw9s/GhIw8cnO61w8v7gzC"
    "+VuiKtsuuoADJQ9ThKBsKBSEQlEo+EpheZHSgzcz+dl/gdFxd0udOUadAVKe33MJHVSyPguju2Dd"
    "ua5dLJ416KaS+eBbWnuPsPbs08Ff060DqDHxHjc9X490m1w63wuK+MPI+S+BJ74VU8Z7eb0OkN9Z"
    "copoTqtgat7BgPDRLBXp9xNQDM/pj2Y9eWbbWEe7kkdudmZw1S5o1dGEZJFqf0OmdlqwxGCtUCiv"
    "YcUlZ3G4BuWCwfMgKBiKgSLVgJHafmb/9g+xftUtYsd9dLqSO1G+8XtRsniuQFWbgvWXwNBKxzYV"
    "vxuhSwHm9xxhxfrVlFedhkaxm8Dvblh3XuIN1V0Gcd1DjSbsuhpZuRp95FuOQ9DlJfb7amHQwp4c"
    "i9GT7JalV1TSEytyJ0ALmaJRInBJHWmbHrPmBcjMQXjy++hFv4BtLsQEiiWQxbgbV8SDtsemHWfQ"
    "3LmOxUVLUBCML3ieqyGMDiuLH/0DFg7PIJWqWxxJ9PJ1WsqNiX246SF/xnPOtm3h1Bt6SGXsCsRE"
    "RCeO0go9Np17ObSG4nmGxhWKpNPalogBOvwAz4d2geDVb0UeuhUWj3RxB8k81642QrKYMyjYFl1C"
    "dmhAJiG5QaB0EaOTq9kxcNSL5k3y6LKNYtZOcQh+9M+w/QpYfoqjZMWLnH77zmmN00gvgFaZHRed"
    "zcGSIQidELQXCEYtleU++vWPc/z7dyDjE3H7XWdhTc/veyUnRIGNC0vxYnlx5N9uQHENbL8ybhhx"
    "WkUqFlhk754pNpy/C8xyly2YYu9ak5F//Lni+UijReHMy1l90blE3/gslANHZc7GXKr9xy3Pp8sA"
    "oC6VVtuB9sHk04k1Abjk9A72RZ79digpn+DSwOzvWfCLyLGn4LFvo1f/OlqbTcwZlDSIFKt/Ih4W"
    "D7+4jIkLT+PAAgy5AaEEWIpjAYWn7uHAJ/4BHR5z6ZbxetU7cVVHShOuL695HIY3x80u0o3oMQW3"
    "CWqLrgq58hRot3p9kH6dIz/ZR7BuA+WVO1BbiDMGk2oVdwIQ8b14gjaqbPvVX8Pe+mP06JNIsdBN"
    "jfOQ0yTZFQYE5TK4HTfdHJppEVfyBSKsKhpF/WlZ0gypDuYEplElBpIGNYTKKPKDv4UN56Fbr0Dr"
    "c3HncOcGTK9vXgyCB42QDRvWEe3aRH1WCTzBMxavYCiF0xz/6/9NFBpnlrs5ebz4fhkKQzD7lKvZ"
    "H70djv/QlWQLI/GMoTjtk3jWULsFp14PpaGYgxAh0iI8dpx567Hp7HOhXYgLP4HrDE4EsaoG8QJ0"
    "LmL19T/DlrPO5PA/fhypKhr1gjRJqq71iWfrgGg/MzUtSxFPjep7DjhAtxqYOxdnqUwwb4ZeOpdN"
    "CUt06NhegCwchx/8Lbzo/b3e9b5Yw/Sg1Yaw45wdHB4qQiNyh06V0qhh4Z/+nPmn9iLVqgsavWLv"
    "ZHYW+PjdMPdUbBnKcOxH8PS/gGnB8Lqe2faCeCqIBW8Udl7ZVSxTDaG9yL4DC2w4extEI4gnMSAU"
    "5/wx1uA2oiDl7Vz7x7/J43/3BaITD8fsprA7qVRTgqkJcR5hgFTMc1SeVNvvkmWgC+gMHfASlbwc"
    "jRnR/PXvBnoZ7f9UjpvMOUOojmPu+zdkaj/6/N+DxRMurdK02oWLhQTfK7Hu6nPZswC+VaLQUlgW"
    "oN/9PMe/9jVkfBmqxvl18WN+3nJonoDD34PGtKvZazw306vC3F64749h6scwtgn8Si9u8HwnKbvi"
    "dNh0BtTn4vbtGQ4+cZTy5o0UR1Zjbew+uilf/Ih9D50e4oo//G2mD8Puf/skZsQ1k9BpTk1KwWhm"
    "AqkmFFj7RIPThy0LwqnSzzJOzHIyAwO8rPxmXz6SKytykoYFzdHRV9dVWxqGr/82bLkeu+NF2PoU"
    "GD/taYxBWyFrVi+nefou5mbj5sxqgH/gKY78w99iq+PuoXsFZ/aDimMVzTziCCXWxhbBTReTmJdH"
    "UHXp3n1/BY9/FEbHXcVR1W0A40O9Dlsvh/GV0KpjTJPwwCFmgiHW7joTGl6syNkJNI2T25+xbPyZ"
    "V7DtZ6/l1t/5C6SwL3ZxYSxLb9N4enJ2oebT7vo2SQIqltxaQSZwjIk3Jreq06lV5yk8ZiqSA5Gp"
    "Adedm0qqgldAaieQm38dnv8BGN3clVOz8S4wIlBrsPPcnRweGcXUQ6wYyn6TqY/8CfXZJlIqx9W8"
    "wJ16P4Aj34XpR+MmDC9uuzIJ8MfrzRj0h+GJL8J33wPBJExs6o6hdzdSguc9H4x1bqA+w7MH26x6"
    "3nZoe7HldNVLvALULcGWs9n557/Fzb/xaZpPfwUCN4m1My7edRRbksrp2fBKTzplK69ApzFlTLtg"
    "WzZ0GCATF8UXZPJd/ED/L0v7KNF8ASmRuCdvFPb/EPnh38ALPoQWhl33TIzVq0Z4WNZdeRFHWlAI"
    "I4Ixj4XPfZKp+36CjC5z1HG/hAytRlrHYf/NUDseM3ViDMF4XbEpMY6oaTCIFwM3wSgcewy+/lY4"
    "cQssX+eAHj8eWTO0DnZdgLYawBxHnzhGZecW/GIZG9kewhi20NIEwV99mDs/+h2O/NffwHCEhs2c"
    "yaaxPF/ye6FfMYYszJIRl0+9b+Knko8LDCgH216gnmV/di5STk5VTs4Klj4ImUR6mBBVLC+Dhz4D"
    "T34dLv0AakquiURAW21WLB+FM89lZloJxorw6N0cu+k/YWQi7i2oQnkFTD+E7v82hG138mNmDonq"
    "oiRVPmN2shHjrqkwAvWmc0v3fQDGhl0Xkxg3Y2D96bByJdKq0dp3kPnKapZv2wL1mntN2AQbIL/8"
    "UerffpS5v/s9ZGTeqVIQxSNt8mYGL2VFZcnTr9mi6iCCzsnSQLWamFWd3lEprHYpWSLJYROlllu7"
    "haIU+cO2kcoyzAMfQQ7cDme+JzbbQL3OzrN2cGT5OpoNCFqTHPvIXxJRjAO9lY76dfCb6LF7e313"
    "XZMvXSp6aixszNTpMdjiOqdfcJ09d34avv4GkKMwusEFjtaHU8+HIISFSY7OFlhzxnZoxlqk1iIv"
    "+Gt4bDf60XfD0CwaK4mYvtnEGUWfzLPupnLx/OI0OT8rMSn9xaglkgUz2F9nRJ+6AwdzDn1ebToB"
    "IGmaGpqLIKYaj2yIFkfgyX+Hfd+CFZei4mFsyNorr2FvXRmqKrOf+Ftqew5DdRiqayGahz1fhoUD"
    "YBz+L/FX75Rn26TTkrci0q3jd9jKFEZh731w0yth6hZYts4pi5bH0dPPgXCSo7tnCc64EC+ouEkh"
    "m1+L7r0T/a9fhVLLEV9sKz75UZ9flbzaieQftixvQgeWZjURxuUBTZKPBGqn+qeaM6wkgcxpYnEH"
    "z4rJDxFTUrSaKPz2NpUEFeTEncjMg2i7xbKJKnrmpcyK4H3/qxz/5g+Q5Rugsg4WnoBnv+TSNa/a"
    "O/WJerykmDnS5T8me9ylUxPA68UIiMsI5hbgX98Md78flo87bacVG5Ct62g+tZvmytMYWb8ZbQsc"
    "uwN+8hHwjbNqhAlL1ytw58vr5c7IdYzhlHSg5iTo/dvBSM4Q7z4LkJrvZNOEbmGAiDEJ2VRZYqdq"
    "wnR1pnT3tzDTjfY17jFUxC9hWidgcZ7tZ57O1KZt+Hue5fA/fxod3+woWftvhgPfi2nZQVw3MolR"
    "bD11UbfwJjO526RH3XeIQkkLoYoUikgwCt/+G/jCq6A46wCizaeAznFs2mf87HOhUUdqTzjGkkbd"
    "6WNCAgvpjKNXfQ7AjuTDuX3/ktEBSk5q1ZzikSSBoFRrV4757+P7KWRFyLOQo2ayg4zymJCHC9iE"
    "Umms/iEeJqqz7JJrOYRh+mMfoa6rkbKgz34R5p4GU4lPbGy6Oxi4STB0EiY++X2fErhIxmp0XIL0"
    "eg0e/R586iUweSs6vAE2r+HE7in09Asd7Csg2gn4rJvbo8mKKOlafd/YtUzsdfJhy4lHrv0offb9"
    "dWCBP59vqolFySWeaFIVvBeRqvZ3Kqlmo1X6hhIk5JeJ2m0mRkcYvv5FHPrcLSzubSFyEH30047K"
    "7VV7pd0E6VKSIpMZcqtImtI2MF5OSeJ3OpliWvqxI/CJn4c7/w+yZTttNYTDm6isWQH1hivxdnUC"
    "O185qKjafBcwaFbTQLaPDCYDaX4MYQalFdpNAwdMp9OMJoj0gwXdSqUmh0EqydFSInlIYu89PCNQ"
    "X2DbBc/jRD1g6gu3wPyd6DNfBfVjrF8Qr9fFI9mBEiLkihlLsuqYGWap/e5ORGKvEluYYhWowFf/"
    "H/jyG2GVMlfaQmHrBgjrXaGN1GnWDBCmJ6dw5G0MhdzunExPSoLl/VzLwZnhSLmeJzX9Mg9Czl5F"
    "zvwh7W+aUO3XJrYIHm3WXvZi7v/U1+Gpm+DIXe7UezERs9NqbTqFF5OYbWjSQyUkXV9LNeP2UbE0"
    "LbggiQKPOJk48TwojqM//CzyT9cxP/840Vk3ItS7sKwMOuHkl1n6Zi3kvFhOAr33ebUBun8DOIFx"
    "NVD75wCnhiP0AY+p0Y89RkwO9VwzgyUkLocmSZEAUavJ0Nr17H1sD8dvei8SHnNCyx0945iJ04N2"
    "JcO/70X+fYMTtC8o7lE4u80YaXazpKxIPEAbC8Vl6P4nsP/nalrze5HxFTFSqEuHeDpQ1TdZs3nO"
    "f1KZevf+zACASAYIRWp2Nkim/6yz4DLY90heM8IA8QPNKxrFFtrzDLVai/u+/HGQCDWVOKhLjFIx"
    "kp47mi2qJ9XLkuRKlURzZf+YnEEReUeeuDO1RDDO1JfG0MVFGp/6ABSHnTKH2u5M5t5k2CWU1SWr"
    "+iKZSl/yKnKke/NEZDVPVkbi1rCcdepAwbJEWpIViO40PPQuTVMRoCRnEeWJF+dwETuonG3UMWJQ"
    "v5R4SL1RMZIaxS79gVHce65q0mxl0VTzSjJO0Ww8nBTJSsQW3bcyTjHEBpVYdzhMTDzRdGmWnOGT"
    "iQOWbxNiNrGRVKSfPVSCJNL3ARlEIrjMFYtWBo0y1xRNSxHUpHPsrIawiMZ178TFxAWY5BDl7mtS"
    "6agjZojnIeo7Aeu4fNxd9I7IhOclqic2rvWbblu0dCyW0t2MHT+qUZgCsoznri+yNm7z73TzKFYt"
    "Xvzz3hyhnlijZy2uh9BNMnfysWmJNmOMc7Ok1XW6qZnnahJ9AtDxwXXt+wnGVt8mWIImmEkJ/RzA"
    "KXabMkAhpDcFQ02ARD60A9Qfddh5u+WKOtECahfdVK6ihykE3VnEtrYYV/l6AyFNqerInl2F0niM"
    "3OJsPPPOw8lDe3iVkd4TMx623XJ8QryYa1hCSlV0cTGm3HfetyPZHia4+OBVh7tm2SnFTsc/j7uE"
    "O3/3CnilEq35ufjfkyGUT0/CmkTaV8SUy6iNWb/GI2zU3T0XionZTD37H9UWiOJ77WEFyV0S4JeH"
    "sFG4JCVPUlZHcklhfvaQm/jk2jBcssIvXoBGRXa98JVsvP5GGuVhgiCgPDRErRESLs7A9BQLjz3E"
    "vm9/nmP7H8OUixgxPP8X3kFh/Ra0UccUA4Iw4K4vf5ZDTz+AXywAQthqsXLdJs6+8VUUR0eQKIRC"
    "QO3gFN+/6Z+IWvN4fkC70WT7OZdwxot+lihq4gUljj9ziHtv/gpXvP5lVLdsoVmr4/keVIaIbJNA"
    "LaENkfoiC8en+cG//jMS1RCUdj3i2pe9kVf/3HUsGx9HRHnsiWe44/s/YO+hYzz68CO86g2vZc26"
    "9YhfphA4EuvUXA3fwMToMK0wJGo3IWpzz70Pcvv3vo1XKCEI7dosb3n729j99G5+8P3vYopD2NjS"
    "qFoKxTI//9rXsG7TVhbrTcTzKJWKoDA/v8h0PeKHd9zB/gduxSsuLSbds9Q27Vi0F5B4IP+753cV"
    "v1BmbPO5LEwdpTl9EOMF+QC+Kngw9+wDHC02OP7KN3F0YoJ9t3+XvT95kGPtiBPbtlG/9gbWXH49"
    "zWfupnHgABL4HHv2MY5c/0b2v/wNHBw7j2c/+lfMPHN3rLneM4OtWo2jU/McevP72XfF1ew/WmHf"
    "J/6C5sIRp1+gYHyPhakpDjYiDvz6n/PscZ9D//JBGosnOHZwH4df+vMcf9UrmVy+g2d27+ZQocIh"
    "U+Tw0DBHr3stB9efRf3fP4QxHmFtgd/9g9/nEx/9M+554Gm+8OWvsW/fCa645ho+8Hu/xsFjU9z2"
    "7a/wb5/5Z1758p+jWZ9j1crlnH/W6bzzba/jyquvJFyYYtmKCXadsplfeddbmZqb55tf/yJ+cYgo"
    "jAgCn3+/6bMsX7WcL3/+P/GCciIOEdCImeOHeee73s4vv+sX2LLjNOqNOsvXbubaF93I617zQq68"
    "7meYOnGMxx+8GxOUlibmKhQqI4haWvXZvLQnBoXFKKCloXHddeNv6eiuG1QKY+oPrVG/ulq96mr1"
    "qmvUq65RU12rprpWZWS9IqNa2rhdh7+2qMVbrHpbLo2jvaqCr/Lej6m5XXXkH+9XU1mpZnidAjrx"
    "ql/Swg9D5c9uc6/3KmrKy9WrrFCvulL96kqlskoD0M1//WXl9lDNK//IAYyVZeqVV7ivygolGFMP"
    "o+Nf2Kv87G+594s/Z9Uv/pYOPxiq9+Fv9ddgt5ymhY//WP2xjYoU9OJLr1JV1T/90D8kXueeyz98"
    "4p/1jz/4YRXQm791q+4689zua8469wKdaoV658NPdF8P6Ove9Iv64Y9/UgEtDq9U8PUFL7pRp0LV"
    "h/ce1w2btiqmrH55XE1xTE1xVP3KhAL69l/6H3o0DPWv//mz8fv5KuXV+ku//Sd6755FvevpKT31"
    "jHMU8dUrjqgJhlJf4lfdV1DVoVU7dGj5po4QcerL5JNIHXbdH5VqyidJPBGkMDSGJ4bQCt74EHhD"
    "yNgE4FP5wecoTEbM+afC+AbXayclCpVhpO0CPIIhJCj1oR+CEJSrDHkFaHqIX+jJyiUKK2J8CiPj"
    "lHH6vCLGDZ+QAuXyMLbpIabkeIfl5Uh1pRNnfPYxzBc/hpQKoC2uue56UOWxB+4GoDq+luLwcsSU"
    "+OCf/RXHDu+nWB7hnz7xKR57+D4KlXGM8RkfG8aKR4jP8OgEJhhC/Cpf/PwX+N6tt2L8MmEYIkZ4"
    "9Wtew769hxhbuZxXvfZ1Tjo3boxNpn7FUpVIPQpBAWN8gkoVrR/jE3/zR+x/+lEoDnPB5de4QpMY"
    "0oX3foKP5os+J6aGJX7BJmjcOghlSrJ1beQmprSJKVGxiAMeI+WAohhYbKHz0900MIoUbYA2bUpv"
    "P11siosGIdAADTt09RxSqiq2CbRsaugloUWbbqKLQfA8HxSGRocYWreVxn//Ozo72X2nOvC6X3wH"
    "a9dtYnH6OM1Gg6A8zLN79/Gxj36MZmj5wmdvwvhVIusGRNjIEkUQRkoUhViriO/TDC1fvOkmMB5R"
    "c5EzzjqH8VXr+N1f/WWILC95+esZGZ0gajb78nar0GpBu+2U222keOVRQhty+NBerFVKxUKq2tpD"
    "nDXziDKzHBNIrslHkmw/tJNLTNduHG+bbkGj+RMQzqAzx1lehbUvfgfzkSA3/yU696xj7nQ48HWg"
    "mdhieXq2AlFnE1jtY6IlccOwHQf4JGblWnVTYZptbGOOaGEGrR1j5XU3Ys55vmtOMQGYErd+61tM"
    "1YVd51zIV755K+/41d9mYvVmWovz2CikZU0cvRdQ4yXSX0cotto7gaKOf2iCCsYrgG3yqte8ljvv"
    "vo+bv/YlfnLv/azbfipXPP8FaLjgUssUBd5Sb0DYDrvzFqL6PBNjYyxfs4m5uXkevOfHPYud14rX"
    "x9qSPrzFDGQcxqdPsqPDM1NDO/CotID5iG2vfh/nvuevuOA3P8zWD9/L3vHLKH/47Zivv98xbju2"
    "JVK0iZv9ktf4mCCchCHQpCsNm7QSXYKFqCNohMnil7MW0RxUhjdy2pt+ix1v+g3OfvP7KLzoN5ib"
    "bmBwVsUvDXHXj37M7/7Ke6jPz7Jh+zbe94E/4kvf+Aa/8ycfYtmaU7DNFsbzugMrkpXGdgjNVphJ"
    "vh1wE7XbTCxbwyVXXsMX/+PTGGP44mc/QxTBS1/+Wjy/0B2Tk0Reaw1LO4wolsqUCx47dmzjj/7i"
    "I6zeuJPPfPTPufuHP0C8cjzjcVDhSHMPbjoNzBR6xHgDVGgyA0tU3aBHVTRUaCoHDs3iTbdQM01t"
    "5kHMzotZ/8pfZ2HNKqZu+lB340RW3ai8tibGzMUmP0svCtVZiyixlxPDkLroXUvdaxPWL1SQOjSb"
    "RQ6uOB8bWiiUWWishVajN4nLWvzKEJ/55N/zwD138JZ3/zpXvuBGNm1ax1vf8XZe8JIbee973skD"
    "t/83XnnYiVpJ18hQb1la7f7U2XgeUWOGn33Fm7j/wUd46tEHAPj3z/wLN77mbZx10ZWcdc553Hf3"
    "XXiloe6tW6vMzs6ycetO/uwjn2ZodJzlK9fwk4cf5L1v/Tnuuv07YAoDizzZzE2TRZcEXD5walg/"
    "miA9JFCT3UGuHVsbbmxL49YPw4Gf9H514/PY965bMFf/EWYB9Gt/7XrnLM4CRBkCCppS1VIUqwJN"
    "3CbL5cM4Z6at2KJ0H4UlbFloQ3TkSWb/8tW9nf/692MnVrvxL+Jk7KOwjVdZxsMPPcJvvOvNnHX2"
    "ObzqjW/l8htewejoGH/6Fx/kTS97iOMnJjG+nzpkzWZIO7R91c4ojPD8Ate86OXcf/99vORnXgam"
    "gG03mTlxiOHVW7j6xa/gvrt/lNr8URQSRfDMk4/z4T/+HX7x3e9lxcZTOXRwL3fdfiueX3HTTuTk"
    "Yo2a1R1MyNiZQWTCLkyabDtSBooN0QbqFqnEApDlFVBege57CHPHB7Eti+58I1oeAuKp3C1nAfKC"
    "OrWKNmooSqMZxS4g1WyWsUSCDQXaUa/3sHM7DVzTRlBxSl9eieiOz6LP/AjxS9j6IqJKuVRCVfFK"
    "w3jFUR584EF+772/zB//5ts5cewIlfE1nHvJZWi44LKMLsZvabXbtFrtuIklHs3uGWxzgcsuvZzR"
    "iZVMLFvO1S99NVe+8OVc97LXc/j4NIf27+e8y29gzfqtRO16l61kI0uz2WR2ZoYTRw/xLx/5c/Y/"
    "9TAXX/sKfu51byMKaxjPX7pPQ7M8i352Zm4QqGqdMvYg0Sb6iw+2HUEYq3bHp9p4AqZAuT2J3xC0"
    "XXC8fWJqWAs3HDCLXKnglwImLryadrPN3Ows0lC0NEGnY7gbI8SjuvxyGZ9hmD2UervIxsFmI45X"
    "NMIEJXTfM7D7YdCAkeedjRp4+y++hbGhIhq6+QVecRi/NMY3b/kad37vG4gfUK4O9z1oEaFWa7BY"
    "a2S2sQAhL3/dm/ny5/6N/+dX38JvveN1/M67XsN73/4qfvudr+HoMw8yNr6SS666DmyzZ9LV0mjU"
    "aTZbiAk4duwoN/3TXxI15rjqpa9l87ZdRK06piN1x8kk5nM1Z/L5ABoPROqVfnRAB5A6RM7iJji2"
    "LEQdpMAjailiWyzbuAutC8zshYWjiPFcdNtSaDldYmsFa5XICrZxglWXvxDdcDU2Cqk9fgfaFFj3"
    "fBhbi12cxOJk2Wxk0PYMyy59MbX5Muz9Puo5MqYYQxi2nQWIP0fVYK1CUEVtyMimCUZe+D5sY4H1"
    "W7dz1bUvxIbzsSZhrO0D1BZmqdUa7HvmiW7qi2psCaBeq1Or1bokFGMMtlVn69btrN58Kl//0k2I"
    "+HiFKiao4hddPePH37uZWm2B0y98PoVSFWsjjPFoRxGNegMbN4/6hSFu+963eeCOWwjKo7z09e/E"
    "80z8+v5B0ktOGUl0kQzsDs52nObvMA/bXsT3Q9cLV7d4QZFiWSkHIROjHme/4Vdo7/wNouMnkO99"
    "AI3aqI0oBAZpCFiPoOJTKlvKZWG4DFsuuRz/6j9m+rEnEDHYO/4defoWGNuBecXfU9mxjZK3QEFm"
    "GS41OfNlr8O77G+pff1/QW0/6lfQKEJthF/ynftoC4WyT7EEpZKhUopYfcZOVr/1Pzj68HHQkNm5"
    "OX75f/4pF112NdqeJ6pPE9UnufCiy7jqJa/ltm9+iYfu/iHGrxBFNg7WWgC0owhrhVariYYNrIJG"
    "dX7uNW/k3vvu59jB3eAVsfEEUVch9Ljl61/h8P5nWLdlF+ddfCVhfQZrHWQcxYukNiKMItQU+Y9P"
    "/h2Th55h51mX8bI3vAsN60StZsLXa34BL+m+NcU9kERB3FKojLDpsjdw5NlHWdh3P15Qykyc6hbA"
    "0Shi+fMuYuzad7I7vIyo0aZUnGSldwDfGoLyKo7Xhph85Dbk3r9Gjz+AV17O1qtfQu3sX+bQ4lbQ"
    "OqPF/QwFbVChUKxQ9zdy+EQR+fLLYO/3naBioQiXvA9OfxujKwwjcw8h4QLl4dVM16oc/doHkUc/"
    "jvrDiDF4hYBt1/0MtbN/jf0nViA0GS8epOi1McajUPBpeqs4urgJ/favY+/7MG9/z29w5sXXEJRG"
    "WJw+yvTRg1SGhjnl9LP53ne/zb/9/Z8xNzfv8PeYBHLF5Rdz48+/keKyLahadj90Ozd/6T/Zs2c/"
    "r33d67n0Ra/m2Wef5Ye3/Cc//OEPCUOLEcFGEcsmRviZl/0cZ11xI83Ip1mb4Ttf/TQeyo2v/kWa"
    "lKjXa9x/+ze449ZvMjk9hW232HrKdl7y2ncwsWoDex67hzu+/TWeeeppxHjxDKS0plNlbA1q29Sm"
    "DyQGRnWdVHIioaVYGWXjpa/lyJ7HWNj3QM4G0J6cio0orN5CO6wQ1qYQo66Xj7iA1FyAxYNIOO0+"
    "yh/GK/hU1p3BwtQCqgtODFpKST4ahE1XwZ07GA90ClxDZVhzrVnLz4HyWrcIs3vg4B1gZ8GrOmgZ"
    "wQQBpbWnsjg5B9KMZ/sU05zFVh1ME2lMQ7tBUAho1edZvmIlp5x2NhMTy1io1XjyJw9y5MBuIED8"
    "Qkw3dzX4jRvXEYZtFhcWEIHR0Qnm5+eYnZthw8bNTE1OUiwUKBaLHDp0lEgVI850D1UrrFy1kqnJ"
    "SSDC8wJK5TKe57G4sICNQoxnKFeHmT4xycLCAsYvYNs1B1UPjVAdGSVstZiamuoWyLokmdiiV8bX"
    "oBpSmzpAggKVJCn1NkBQGmLDJa/h+L6nWNj3QFyzHsAoESFqLiRq+yST9a5kivrFRAuWi4yTrwPb"
    "pzDkVNZKiB+koEttLfTQns6nmiJqik5ipdMGZi22tdAFg5I8Q8nyGI2PmEIXWNKwmbk+g/FLvebt"
    "7lw/wbZqCW6Axr8Xy8x0n0tser1yKnK3UZR5dklNP5P4XQsSi2N2rG+HdUTkXusFpOcT9N6yPL4G"
    "tO02QGYR/VR5UMFGbaJ2AxMEOToAWY6kxRQroJU4KOrnsabkzuKihSmPuGxBbS7ElIyhex298c8K"
    "Q70qhWqSR9ETZ8b1CJjSiBv3ktTbTfHk02K3RuLsolB2o2skOSJXex3GiYfgFSuxa41SM7UBJBhK"
    "jeDtSr51VDqNB6aaUgiVeD0099D1MgSRWAElketLHpTe6Y5KspISuI6fVwm07Tp+odJblCTNVKWP"
    "WaTdhYIBjYK9cMR43Xm8iIcxEg+rtl2Kt1Xr6GQdkxY/LFXr6gGd3yHCxJQ0jQWUTYLJpNYmGjrc"
    "A1KrMaM4bpyMh1V4xsRd0da9Z3xCO4Mnuyq/YrrSt70Hbrs9hDZeTBOrkdmos9imS6ztLJYYiOLJ"
    "LMZ48T1pih2U5Ul2fqYZa5a7+CjG+G40X5ig3CU2qZ9hPWKjkHZtBhOMuyKJ2u4oOLLa/yRrBdJL"
    "0TNVw84HR5GFxjwYH1MsYqMI22iAX8AEAVEUQWsh/r5A1GxhCk4mJqwtQKGE5/ku6vUMxvhEzYYr"
    "6AQljOcR1utOyAHBCxyPsDtfutXCFAvYlmvfxvecObfQbs6DKWCCIlGz7tLIghssaFttTFAAgbDV"
    "dEMsxRC1nSsyQXxN2kb8MmKEsNWIf1bE2gjbbrgmFhRjPKJWw7kyv+z0MZo115Aaf07HDSj5jF5n"
    "Ue3gDqKYwyiFAsYYwiiMf+6lDrVjBAmp0S5BaZjCyGpqM0fRdqNHayZLVJd0cTjW4HWSrnGHbYcx"
    "HEWMjgxx5fOvxfcDJo8doTo0xOVXXUmkMDd1gonxMa669jpqizXmZyfZeeqpzC8sYIzHlVdfRW1h"
    "nsWFGbaesh0bRTRrc+w6/QzOOf98Dh8+SthscNZ554ONGB4eigMzd/KKBZ+t27YyeeIY69dvYN3G"
    "dXieR7vdolQscu0LX4QYj6kThzhlx07OOOtsDh3Yjx8U2LptK1NTk6Bwyo7tNBp12s0a69avY3h4"
    "mLmZSTZt2cz5F17M0SOHaLcanHHWOWzbvp0D+/cxNDTEaaefjgKFQpF6bY7tO0/l3PMv5NDBw0RR"
    "m4svu4KR0TGOHzviMAiJSaxeIZa88dOaBoaEz5d85qdaCpVR/EKR9uIMUdhMTyPpIoGZTdaYm0Q0"
    "xB+aSPuOjD6Q4CGFZTB0KmbiUmTVC5FNr0DOeCNy6dvhqndiL34jUhpHW022bt5MtVLm3PPOZXh4"
    "iLGRIa69/oXuLcMGL73xJUyfOEq90UQj4aJLLqJcLFAueFx00cVs2LgRbdc474LzGRtfxsjYMi64"
    "8HzazRovuOEGNFzgsksv5W3vehcXXHgBtlV3cKwqlXKR5197DRrW2bhpA6994y/wspe/glKpRH1h"
    "jk1bNrsJpmGDiy+7gu3bd9Bu1hkeGeW8Cy5AwzYahpx3wXmUy2U0arBr1w5O2b4V3yjXv+AGCoUC"
    "O09/Hho1ufyKK1i/cTMathkarvLy17ye17zhTVSrVarVKs9/wQ08u/tphIhi4HPdDS8iKBbjCqyC"
    "X0UueR3yol9GXvxrcO274YI3I6e8All5LTJyDlLeAF41U+ZNgHRiCGJtwyhsplm/6cmh6e3TXDhB"
    "VJ+hNLyc1vFne4FSZgKlagitSWhNoeLHitiBU9SOp4aLNt1cIC/gxNQkZ5x9Dvv27qa2WKNQrLB3"
    "z7NONEGEnzzyGKds387hIyeYPn6Y/fsPEkaWZqPB008/xdDQMIjh0IGDNJtNavMzHD12nInlq3nw"
    "wQfAFLjzR7ezbOVajGfiIM7t2nYY8uzu3SAekyeOc8vX/4tTTzuDdqtBFIU89vDDTE1Pgxj27H6G"
    "1WvWUSgNszA3RVAoc8qpZ/D0Ew8wNzvP2eecw63/vZ+pqWmsdX78iSceZ9uOUzl0+AiIYf/+fRRL"
    "FcQERGHI1776ZXbsPJUwimjUF9mz+1lO2b6TQ4cOEoXKvj3P9iRzRNCwhtzzFdQUYmsK2BCN2jHt"
    "PXQuJCPLlwzQvaCEeAG2vuBIqp3Tr72GmTg6IlGJcqZjbMMZBMu2Mb3nEbQ+6dIMzZtQIUtWoVKz"
    "7aylWi5Qqze6/q0YGBSPVhii7RbDoyM0Gi0XHMURq/EMxcBjcbHufLGNupGyAcrlIgtzC5hCEQGi"
    "ZgPxfYcJJGVXrQvWDBDZCA1beMWSCwTj9+x8ZrlcoNly7J5iwaVfzVYbzxiKxcDdQwy4KIqNIiqV"
    "ErX5RUyhiEEplYvxvcZBa9jEK5Sw6jgIw0NDLCzWHePPgyAoUG80Y73kRC0/q9UomRirr39AUBtS"
    "Gl1FoTJEa/YIjYWpXpteOpwziamOnWYNS6EyzvgpFzM/v0j9wEMuLdT8KTWam8Zlesel13doPJNp"
    "NdduC3YUWcRLdNQk0rB0BU66zSORRi6K18yVaP+VSVp4x7k40V7AFV+PtW7jSfz3zvwk7fzM9Gsj"
    "R2rxOtmKxvfamSTW6dFL9j2EUfxzRy1DbQ9j6CZcedNadQCJJ4GrGJ/qis0Y22D++F5s2M7JJpIx"
    "QJJrJYZWfYbWzCGK1WGkutyNU5M8QZK4ni8D7IFo4tfiB2d7M+zF9MQXOmmcpNQve2lOf5u5I6R4"
    "qbxZu8obkuziItPY2ZkG1u0RShMmPGPizjFxvRIJ0avk4kviSRiRnsJanPqlDmliJiJxmtvZp6Yj"
    "KduHnyTlczQtJdf3nLRbqCoMTWAE2vV5bGcwl6QCuCQfQBJzZHoXuHB8D15Yo7h8k5vqbTX9oDMi"
    "ximFcAbLxWS7x/tkTTJiHZI3jSRBdtSclvOl2+0Hz+SVvs2dPXPph6/ZPm7tlyHIi9E09Qwl8cz6"
    "K/cm1Te+RA+ACKoRfqFKsToG4SLNhelMpTBdORygEOKsQLs+S3P6IIVCgBnfiI1a3ZOYnEPc499p"
    "39QAyX0yA+rVeQ9Q0z3EIhlvlR2yIJISV88lSvQ3g5NpVexf8NylGfCeKXHjHAvbsU4ptu5gDcDs"
    "9PelZkKKQmXZOgRLqzaXSf3SWVx6bJxov7KACAsn9sDiCUojK5DhtWjUSnQCJ3ay0td3AUn92vzF"
    "0Jz25WSzt5Lsok2rj2iGGCapU5W+vufcXJ8Ussy5ph6iKYnNJvmnnXzCrSbL7kmthD7nmoRusyX7"
    "viYKNGxTWebwDW3N05if6sdwMn81JMmYfQwgg0YtasefoqANguWb0PJyN/I0hkvzSUMyqHwwyCJ2"
    "MXDyIE1Nw6E9+FNTFjzzsucorqCpmkRKbHHAiVeeI+EiCdDoYHvUv2/yFFfpm0CaMv1hi9LYaoLK"
    "CIQNFqeP9GT6cyDgBJ4k/bhzL2pxrqA2S/3o45S0QbByB1pZEY93kYF959mqW7+7lYzJlcwpTrfi"
    "5z7jrpZBUn9HsnTR57ANJK1oJnlNMcmr7tsO/Vt80C6UwVurfyZUxn7JAL8ftiiOraUwvAyJmizO"
    "HHZQdN9EVvIkH1Nheh+vPQkrlsfXU1ixnYaUaE/tQ2f3xSVKk/Yx5A8Y15yODiEphZL+rY7r6Ap6"
    "pIKyfuWs7nQB6c0mUgaoleQyoQQVN62cAa5LM5H/AOef76hVl57pzFLiu2SYPBKP94soT6ynUB3H"
    "2DqN6cPUF6Z7I39OvvVF+0aOpEyFJlgklvLYaoqrdtIwQ7TmTriR61GrK9DYbd6QnBtPqYf0hxzJ"
    "Re8flZ4xi/F7KvmyM13tiMzGyc0CUnFo4qQnqqCa3XZ5/QspjcRB8e7SKmC5mIr235jaNl6hTGXZ"
    "Bjw/wERNajOHaTyXxU9TwozmxQiaPTVdiR9LUBmjvGoHUWGCZquJnTsMi8ficSqJkWyZwRApQcbE"
    "adCMjFDaUAyAmQZIpAxiLZPdWL20IT3/SAZx6ulfFGFwWL7k5PZB0/xyNlTK5FgnsuEVKI6upDS8"
    "DKNttFVjceoQrcZCPBNZE3I2gxc/fj5G84cPav/DSmwCExSpLt+CGVlNS8qEzTp2/iham3QWIfmA"
    "RfKDMtW0C5YspSA7p3ypE6cDDPWAYCpv4STH96rms5U4ibZvYkN1eAAysIlH8/queqBWDIubQolC"
    "dRy/MornOfXUsD7L4vQxbNTule2V/GA6dz+K0ZOnRdm9YLoCiIXhZZRG10J5glBKRFEL21xEG3NO"
    "NbvddPSl1EnWJQRsMvBx8nU2aSbSsYRmFq/rbJKZg0qaxZJCsEzmPbWfQZPUNBoU2+S5AnRAwKwJ"
    "lLNnpTRmNIkYxCvgFyuY0hB+oYzvGWjXiJoLNOYnadXn6UriDRoty+Cz0bcBhL66Ql+A14sJhI7W"
    "TqE6RmFoBV51AvXLaDzqxUYxd63TxqTxSsbdPEb6Wymys26V9PConkab7SFp0puYEb9zWq1MOllH"
    "33SM+NlpV3BK+vyQZmhdoKIZtTPtwbtKzAzquL5MDpYoOpGktnXuU9zoGROPnjciiA0hbBA1F2jW"
    "ZmkuzsX37qVqLTk7Mz/+lUEW4GSAXepkSnqSCGD8IoXyCKZYwQRlxCu4iZkdRc8kf73zcIzEkXey"
    "shWrhcecQO0qjAuaiVOkLw7QdEtjOkfohXNJaZZusJheLIkHaGhcqBFivF/6XUlXkrBb6IqHZhqT"
    "4RYm46DefCbpXpc7BGotNmwStRpErRrtZo0obCWssKQXfwnEe6AiX3IDpKzjAAHJdOieVALvkCht"
    "yumKeE52zXj9JlPpZ7GmLKSmtPg0kU1kAz0yMzYHRwL95WoVOenr0ry7hBvQHomzcy/ZqmQqy9Z0"
    "faFv0mti81gbZRRbJUHPWyIsWuoBLOUC5DnNDNb0zlPJQZgyJu65QrH/90//SkqierlkzMRz15RN"
    "bAJ/qSrzYOiADE05W2TQTJom6QBDnkMatYT7yk1UTtYmrQPQqZx0d8mWe3luwdVJF2bg9fTXk5Ac"
    "GE9ZcibUc70O/+Q57iC/Iie5IRnQpCiZ7pSkMAT9AwYyOEEfO1kHZS2SfyOag8qJnMTVkd8j8Vx2"
    "gQzM9NKFgOw+zJ/CtSTQ+Jw2XX8xiCWrRUuCGwPoyH031De8TtKl0lxwZ1CqLfkpYirdygGH8tyU"
    "5sQ4kqVg6cmrWZK32pqPrehJFkUzKGy2TK454+JFT26lJH9Dmp/OPWsXOT75uFjNbxARco7AIGAl"
    "FwIcAFoN+F41v3yK9qdjfcWw/Nt6TogfS1gMOclBk5N6RZYsLbL0siT/7f8F9OOC4x4Pk/QAAAAA"
    "SUVORK5CYII="
)

_LOGO_INTEGRE = io.BytesIO(base64.b64decode(_LOGO_B64))

st.set_page_config(
    page_title="BLUESTAR DESK - Decision Intelligence",
    page_icon="⭐",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Habillage
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
  .stApp { background: linear-gradient(180deg,#050B1A 0%,#0A1530 100%); }
  #MainMenu, footer, header { visibility: hidden; }

  .bl-hdr { display:flex; align-items:center; gap:18px;
            padding:22px 0 18px; border-bottom:1px solid rgba(90,140,255,.22); }
  /* Logo BLUESTAR, affiche par st.image et non par du HTML : une image
     ne traverse pas le moteur Markdown du navigateur, elle s'affiche donc
     toujours. C'est ce qui manquait au SVG dessine en HTML. */
  .bl-logo { width:52px; height:52px; border-radius:10px;
             overflow:hidden; flex-shrink:0;
             border:1px solid rgba(90,140,255,.30);
             background:rgba(255,255,255,.04); }
  .bl-logo img { width:100%; height:100%; object-fit:cover; display:block; }
  .bl-nom { font-size:25px; font-weight:700; color:#EAF1FF;
            letter-spacing:2.4px; line-height:1.15; }
  .bl-sous { font-size:12.5px; color:#6C86B8; letter-spacing:1.6px;
             text-transform:uppercase; margin-top:3px; }
  .bl-hash { margin-left:auto; text-align:right; font-size:11px;
             color:#4E63A8; }

  .bl-msg { margin:26px 0 0; font-size:15px; color:#9FB4D8; }
  .bl-msg b { color:#EAF1FF; }

  .bl-etape { display:flex; align-items:center; gap:11px; padding:9px 14px;
              margin-bottom:7px; border-radius:9px;
              background:rgba(255,255,255,.028);
              border:1px solid rgba(255,255,255,.05);
              font-size:13.5px; color:#B9C8E4; }
  .bl-etape.ok    { border-color:rgba(0,208,132,.42); background:rgba(0,208,132,.07); }
  .bl-etape.echec { border-color:rgba(255,86,86,.45);  background:rgba(255,86,86,.08); }
  .bl-pastille { width:9px; height:9px; border-radius:50%;
                 background:#3A4A68; flex-shrink:0; }
  .bl-etape.ok    .bl-pastille { background:#00D084; box-shadow:0 0 9px #00D084; }
  .bl-etape.echec .bl-pastille { background:#FF5656; }
  .bl-etape-msg { margin-left:auto; font-size:12px; color:#7C90B4;
                  font-variant-numeric:tabular-nums; }
  .bl-duree { float:right; font-size:12px; color:#7C90B4;
              font-variant-numeric:tabular-nums; }
</style>
""",
    unsafe_allow_html=True,
)

_col_logo, _col_titre = st.columns([1, 14], gap="small")

with _col_logo:
    # Ordre de preference : le .ico (264 Ko, 256x256), puis sa copie PNG,
    # puis l'original 1254x1254 (1,4 Mo). Les trois montrent le meme
    # visuel, verifie : ecart de 0,05 % entre le .ico et le PNG 256.
    # On prend le plus leger d'abord, car le poids est ce qui avait fait
    # echouer le televersement depuis le navigateur.
    _dossier = Path(__file__).resolve().parent
    _logo = None
    for _nom in ("logo.ico", "logo_256.png", "logo.png"):
        _candidat = _dossier / _nom
        if _candidat.is_file():
            _logo = _candidat
            break

    st.markdown('<div class="bl-logo">', unsafe_allow_html=True)
    if _logo is not None:
        st.image(str(_logo), width=52)
    else:
        # Le logo est aussi integre ici, en clair : il s'affiche donc meme
        # si aucun fichier image n'a pu etre depose dans le depot.
        st.image(_LOGO_INTEGRE, width=52)
    st.markdown("</div>", unsafe_allow_html=True)

with _col_titre:
    st.markdown(
        """
<div class="bl-hdr">
  <div>
    <div class="bl-nom">BLUESTAR</div>
    <div class="bl-sous">Decision Intelligence</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Verification des identifiants
# ---------------------------------------------------------------------------
try:
    ident = lire_identifiants()
except Exception as exc:
    st.error("Impossible de lire les secrets : " + str(exc))
    st.stop()

manquant = [n for n, v in (("OANDA_ACCESS_TOKEN", ident.jeton),
                           ("OANDA_ACCOUNT_ID", ident.compte)) if not v]

if manquant:
    st.warning(
        "Identifiants OANDA absents : **" + "**, **".join(manquant) + "**\n\n"
        "Renseignez-les dans **Settings -> Secrets** de Streamlit Cloud :"
    )
    st.code(
        'OANDA_ACCESS_TOKEN = "votre_jeton_oanda_practice"\n'
        'OANDA_ACCOUNT_ID   = "101-002-00000000-001"\n'
        'OANDA_ENV = "practice"',
        language="toml",
    )
    st.caption(
        "Ces cles sont celles attendues par vos 4 scanners d'origine (GPS, "
        "CHoCH, RSI, S/R). Le Calendrier, le Merge et BLS V10 Pro n'ont "
        "besoin d'aucun secret."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------
if "resultat" not in st.session_state:
    st.session_state.resultat = None
if "quand" not in st.session_state:
    st.session_state.quand = None

col_g, col_i = st.columns([1, 3])

with col_g:
    lancer = st.button("Lancer le desk", type="primary", use_container_width=True)

with col_i:
    if st.session_state.quand:
        st.caption("Derniere execution : " + st.session_state.quand)
    else:
        st.caption("Aucune execution depuis l'ouverture de cette page.")

if lancer:
    zone = st.container()
    with zone:
        st.markdown("### Execution en cours")
        barre = st.progress(0.0)
        texte = st.empty()
        liste = st.empty()
        chrono = st.empty()

        etats = {c: ("en_attente", "") for c, _ in ETAPES}
        debut = time.monotonic()

        def rendre():
            lignes = []
            for cle, libelle in ETAPES:
                statut, message = etats[cle]
                cls = "bl-etape" + (" ok" if statut == "ok" else
                                    " echec" if statut == "echec" else "")
                lignes.append(
                    '<div class="' + cls + '"><span class="bl-pastille"></span>'
                    "<span>" + libelle + "</span>"
                    '<span class="bl-etape-msg">' + message + "</span></div>"
                )
            liste.markdown("".join(lignes), unsafe_allow_html=True)

        def progression(cle, etape):
            etats[cle] = (etape.statut, etape.message or "")
            if etape.statut == "en_cours":
                n = sum(1 for c, _ in ETAPES if etats[c][0] == "ok")
                texte.markdown(
                    '<div class="bl-msg"><b>' + etape.libelle + "</b> - "
                    + (etape.message or "en cours...") + "</div>",
                    unsafe_allow_html=True,
                )
                barre.progress(min(0.95, (n + 0.4) / len(ETAPES)))
            rendre()

        rendre()
        resultat = executer_chaine(ident, progression=progression)
        st.session_state.resultat = resultat
        st.session_state.quand = datetime.datetime.now().strftime("%d/%m/%Y a %H:%M:%S")

        duree = time.monotonic() - debut
        if resultat.statut == "ok":
            barre.progress(1.0)
            texte.markdown(
                '<div class="bl-msg">Desk genere en <b>' + "{0:.1f}".format(duree)
                + " s</b> - " + "{0:,}".format(resultat.desk_octets).replace(",", " ")
                + " octets</div>",
                unsafe_allow_html=True,
            )
        else:
            texte.markdown(
                '<div class="bl-msg" style="color:#FF8A8A">Arret de la chaine : <b>'
                + resultat.erreur + "</b></div>",
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------------------
# Resultat
# ---------------------------------------------------------------------------
res = st.session_state.resultat
if res is None:
    st.markdown(
        '<div class="bl-msg">Cliquez sur <b>Lancer le desk</b> pour executer la '
        "chaine complete : 4 scanners en parallele, calendrier, fusion, puis le "
        "desk BLS V10 Pro.</div>",
        unsafe_allow_html=True,
    )
    st.stop()

st.divider()

if res.statut == "ok" and res.desk and Path(res.desk).is_file():
    octets = Path(res.desk).read_bytes()
    c1, c2, c3 = st.columns(3)
    c1.metric("Duree totale", "{0:.1f} s".format(res.duree_s))
    c2.metric("Taille du desk", "{0:,}".format(len(octets)).replace(",", " ") + " o")
    c3.metric("Empreinte SHA-256", res.desk_sha256[:12])

    st.download_button(
        "Telecharger le rapport technique",
        data=octets,
        file_name="Rapport desk technique.html",
        mime="text/html",
        use_container_width=False,
    )

    st.caption("Apercu du desk")

    # On affiche le desk dans une IFRAME et non par st.html.
    #
    # Pourquoi : st.html passe le contenu par DOMPurify en profil « html »
    # (USE_PROFILES: {html: true}), qui SUPPRIME les balises SVG. Le logo
    # en etoile bleue du desk disparaissait donc de l'apercu, alors qu'il
    # est intact dans le fichier telecharge - c'est exactement le cadre
    # blanc sans etoile observe.
    #
    # st.iframe depose le HTML tel quel dans une iframe : le desk s'y
    # affiche complet, avec son logo et ses styles.
    st.iframe(octets.decode("utf-8", errors="replace"), height=900)
else:
    st.error("Chaine interrompue - aucun desk n'a ete produit.")
    st.markdown("**Cause reelle :** " + (res.erreur or "inconnue"))
    st.info(
        "Aucun desk partiel n'est presente : une etape a echoue et la chaine "
        "s'est arretee. Corrigez la cause, puis relancez."
    )

with st.expander("Detail des etapes"):
    for e in res.etapes:
        duree = "" if e.duree_s is None else "  ({0:.1f} s)".format(e.duree_s)
        symbole = {"ok": "[OK]", "echec": "[ECHEC]"}.get(e.statut, "[--]")
        st.markdown(
            "`" + symbole + "` **" + e.libelle + "** - " + (e.message or e.statut) + duree
        )